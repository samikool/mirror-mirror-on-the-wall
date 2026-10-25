#!/bin/sh
# Installs mmw on this machine from its installer, offline.
#
#   tar -xf mmw-VERSION-ARCH.tar && sh mmw-VERSION/install.sh [--prefix DIR]
#
# Beside this script: one directory per architecture, each with uv, a
# standalone Python archive and mmw's wheels. The interpreter and a
# virtualenv go in PREFIX/share/mmw/VERSION-pyPYTHON-BUILD-ARCH, the
# launcher in PREFIX/bin/mmw; PREFIX/share/mmw/CURRENT records
# "VERSION PYTHON NAME". PREFIX defaults to ~/.local. Nothing is
# downloaded, and the system Python is never used. mmw itself knows
# nothing of mirrors until it is pointed at one.
#
# POSIX sh and utilities only (tar, gzip, uname, cksum, cp, mv).
set -eu

usage() {
    echo "usage: sh install.sh [--prefix DIR]" >&2
    exit 64
}

prefix=
while [ $# -gt 0 ]; do
    case $1 in
        --prefix) [ $# -ge 2 ] || usage; prefix=$2; shift 2 ;;
        -h|--help) usage ;;
        *) echo "unknown option: $1" >&2; usage ;;
    esac
done
[ -n "$prefix" ] || prefix=${HOME:?HOME is not set — pass --prefix DIR}/.local
# One spelling of the path, so the PATH check and the launcher agree.
while :; do
    case $prefix in
        *//*) prefix=${prefix%%//*}/${prefix#*//} ;;
        ?*/) prefix=${prefix%/} ;;
        *) break ;;
    esac
done
case $0 in */*) from=${0%/*} ;; *) from=. ;; esac
case $prefix in /*) ;; *) prefix=$(pwd)/$prefix ;; esac
# The launcher holds PREFIX inside double quotes: refuse what would end them.
nl='
'
case $prefix in
    *[\"\$\`\\]*|*"$nl"*)
        echo "--prefix $prefix: the launcher can't hold a path with \", \$, \`, \\ or a newline — pick another" >&2
        exit 64 ;;
esac
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
    echo "this installer is for: ${have:-none} — this machine is $arch" >&2
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

share=$prefix/share/mmw
# The build id (the mmw wheel's cksum) tells a rebuild of the same version
# apart, so it installs beside the one in use instead of over it.
build=$(cksum < "$wheel")
build=${build%% *}
name=$version-py$py-$build
dest=$share/$name-$arch
cache=$share/.uv-cache
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

mkdir -p "$prefix/bin"
launcher=$prefix/bin/mmw
cat > "$launcher.tmp" <<EOF
#!/bin/sh
# mmw $version (Python $py), installed by install.sh.
arch=\$(uname -m)
case \$arch in arm64) arch=aarch64 ;; esac
mmw="$share/$name-\$arch/venv/bin/mmw"
if [ ! -x "\$mmw" ]; then
    echo "mmw $version isn't installed here for \$arch — run install.sh from mmw-$version-\$arch.tar" >&2
    exit 2
fi
exec "\$mmw" "\$@"
EOF
chmod 755 "$launcher.tmp"
mv -f "$launcher.tmp" "$launcher"
echo "$version $py $name" > "$share/CURRENT.tmp"
mv -f "$share/CURRENT.tmp" "$share/CURRENT"
echo "mmw $version installed — $launcher"
case ":${PATH:-}:" in
    *":$prefix/bin:"*) ;;
    *) echo "add it to your PATH: export PATH=$prefix/bin:\$PATH" ;;
esac
