#!/bin/sh
# Installs mmw on this machine from a bundle's runtime, offline.
#
#   sh bootstrap.sh --home DIR [--from RUNTIME_DIR]
#
# RUNTIME_DIR (default: this script's directory) holds one directory per
# architecture, each with uv, a standalone Python archive and mmw's wheels.
# Everything goes under DIR: the interpreter and a virtualenv in
# DIR/runtime/VERSION-pyPYTHON-BUILD-ARCH, and the launcher DIR/bin/mmw (it runs
# this machine's architecture's install). DIR/runtime/CURRENT records
# "VERSION PYTHON NAME". Nothing is downloaded, and the system Python is
# never used.
#
# POSIX sh and utilities only (tar, gzip, uname, cksum, cp, mv).
set -eu

usage() {
    echo "usage: sh bootstrap.sh --home DIR [--from RUNTIME_DIR]" >&2
    exit 64
}

home=
from=
while [ $# -gt 0 ]; do
    case $1 in
        --home) [ $# -ge 2 ] || usage; home=$2; shift 2 ;;
        --from) [ $# -ge 2 ] || usage; from=$2; shift 2 ;;
        -h|--help) usage ;;
        *) echo "unknown option: $1" >&2; usage ;;
    esac
done
[ -n "$home" ] || usage
if [ -z "$from" ]; then
    case $0 in */*) from=${0%/*} ;; *) from=. ;; esac
fi
case $home in /*) ;; *) home=$(pwd)/$home ;; esac
case $from in /*) ;; *) from=$(pwd)/$from ;; esac

arch=$(uname -m)
case $arch in arm64) arch=aarch64 ;; esac
rt=$from/$arch
if [ ! -f "$rt/uv" ]; then
    have=
    for d in "$from"/*/; do
        [ -f "${d}uv" ] || continue
        d=${d%/}
        have="$have${have:+, }${d##*/}"
    done
    echo "this bundle has runtimes for: ${have:-none} — this machine is $arch" >&2
    exit 2
fi

version=
for w in "$rt"/wheels/mirror_mirror_on_the_wall-*.whl; do
    [ -f "$w" ] || continue
    wheel=$w
    version=${w##*/mirror_mirror_on_the_wall-}
    version=${version%%-*}
done
if [ -z "$version" ]; then
    echo "$rt/wheels has no mmw wheel" >&2
    exit 1
fi
archive=
for a in "$rt"/*python-*.tar.gz; do
    [ -f "$a" ] && archive=$a
done
if [ -z "$archive" ]; then
    echo "$rt has no Python archive" >&2
    exit 1
fi

py=${archive##*/cpython-}
py=${py%%[-+]*}
py=${py%.*}

# Keyed by architecture too: DIR may be on a share that machines of several
# architectures mount, each running its own install through one launcher.
# The build id (the mmw wheel's cksum — the same wheel for every
# architecture) tells a rebuild of the same version apart, so it installs
# beside the one in use instead of over it.
build=$(cksum < "$wheel")
build=${build%% *}
name=$version-py$py-$build
dest=$home/runtime/$name-$arch
cache=$home/runtime/.uv-cache
# The interpreter and the virtualenv record their absolute paths, so they are
# built where they stay; `.complete` marks a finished install, and a failed
# one is removed whole. The launcher changes only after it completes.
cleanup() {
    status=$?
    rm -rf "$cache"
    if [ $status -ne 0 ] && [ ! -f "$dest/.complete" ]; then
        rm -rf "$dest"
    fi
    exit $status
}
trap cleanup EXIT
trap 'exit 130' INT TERM

if [ ! -f "$dest/.complete" ]; then
    rm -rf "$dest"
    mkdir -p "$dest"
    cp "$rt/uv" "$dest/uv"
    chmod 755 "$dest/uv"
    gzip -dc "$archive" | (cd "$dest" && tar -xf -)
    if [ ! -x "$dest/python/bin/python3" ]; then
        echo "$archive has no python/bin/python3" >&2
        exit 1
    fi
    UV_CACHE_DIR=$cache UV_NO_CONFIG=1 UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never
    export UV_CACHE_DIR UV_NO_CONFIG UV_OFFLINE UV_PYTHON_DOWNLOADS
    "$dest/uv" venv --python "$dest/python/bin/python3" "$dest/venv"
    "$dest/uv" pip install --offline --no-index --find-links "$rt/wheels" \
        --python "$dest/venv/bin/python" "mirror-mirror-on-the-wall==$version"
    if [ ! -x "$dest/venv/bin/mmw" ]; then
        echo "the install made no $dest/venv/bin/mmw" >&2
        exit 1
    fi
    cp "$wheel" "$dest/mmw.whl"
    echo "$version" > "$dest/.complete"
fi

mkdir -p "$home/bin"
launcher=$home/bin/mmw
cat > "$launcher.tmp" <<EOF
#!/bin/sh
# mmw $version (Python $py), installed by bootstrap.sh. The site's home unless told otherwise.
: "\${MMW_SITE_HOME:=$home}"
export MMW_SITE_HOME
arch=\$(uname -m)
case \$arch in arm64) arch=aarch64 ;; esac
mmw="$home/runtime/$name-\$arch/venv/bin/mmw"
if [ ! -x "\$mmw" ]; then
    echo "mmw $version isn't installed here for \$arch — run bootstrap.sh from a bundle with an \$arch runtime:" >&2
    echo "  sh .mmw/runtime/bootstrap.sh --home $home   (or sh $home/runtime/incoming/bootstrap.sh --home $home)" >&2
    exit 2
fi
exec "\$mmw" "\$@"
EOF
chmod 755 "$launcher.tmp"
mv -f "$launcher.tmp" "$launcher"
echo "$version $py $name" > "$home/runtime/CURRENT.tmp"
mv -f "$home/runtime/CURRENT.tmp" "$home/runtime/CURRENT"
echo "mmw $version installed — $launcher (export PATH=$home/bin:\$PATH)"
