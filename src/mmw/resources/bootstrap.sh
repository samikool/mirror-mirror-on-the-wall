#!/bin/sh
# Installs mmw on this machine from a bundle's runtime, offline.
#
#   sh bootstrap.sh --home DIR [--from RUNTIME_DIR]
#
# RUNTIME_DIR (default: this script's directory) holds one directory per
# architecture, each with uv, a standalone Python archive and mmw's wheels.
# Everything goes under DIR: the interpreter and a virtualenv in
# DIR/runtime/VERSION, and the launcher DIR/bin/mmw. Nothing is downloaded,
# and the system Python is never used.
#
# POSIX sh only; needs tar, gzip and uname.
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

dest=$home/runtime/$version
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
    echo "$version" > "$dest/.complete"
fi

mkdir -p "$home/bin"
launcher=$home/bin/mmw
cat > "$launcher.tmp" <<EOF
#!/bin/sh
# mmw $version, installed by bootstrap.sh. The site's home unless told otherwise.
: "\${MMW_SITE_HOME:=$home}"
export MMW_SITE_HOME
exec "$dest/venv/bin/mmw" "\$@"
EOF
chmod 755 "$launcher.tmp"
mv -f "$launcher.tmp" "$launcher"
echo "mmw $version installed — $launcher (export PATH=$home/bin:\$PATH)"
