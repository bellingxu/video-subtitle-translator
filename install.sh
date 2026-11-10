#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────
#  一键安装“字幕翻译工具”的运行环境  /  One-step installer
#
#  做的事情：把 Python 3.12、ffmpeg（带字幕渲染）和语音识别库装进本文件夹的
#  runtime/ 里（约 1 GB）。不需要管理员密码，不改动系统里的任何其它软件，
#  想卸载时把整个文件夹删掉即可。
#
#  用法：
#    bash install.sh            安装（已经装好就什么都不做；更新后再运行一次即可）
#    bash install.sh --force    删掉旧的 runtime/ 重新安装
#
#  网络慢的时候可以换下载源（见 README 的“常见问题”）：
#    CONDA_CHANNEL=<conda-forge 镜像地址>  PIP_INDEX_URL=<PyPI 镜像地址>  bash install.sh
# ─────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")"

CHANNEL="${CONDA_CHANNEL:-conda-forge}"
MICROMAMBA_URL="${MICROMAMBA_URL:-https://micro.mamba.pm/api/micromamba/osx-arm64/latest}"
TMP="$PWD/.install-tmp"
PY="./runtime/bin/python"
STAMP="./runtime/.requirements.txt"

step() { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✔ %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31m✘ %s\033[0m\n' "$*" >&2; exit 1; }

cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

# 运行环境是否完整可用：有 Python、有带字幕渲染功能的 ffmpeg、语音识别库能载入
runtime_ok() {
  [ -x "$PY" ] && [ -x ./runtime/bin/ffmpeg ] || return 1
  local filters
  filters="$(./runtime/bin/ffmpeg -hide_banner -filters 2>/dev/null)" || return 1
  grep -q ' subtitles ' <<<"$filters" || return 1
  "$PY" -c "import mlx_whisper, numpy, huggingface_hub" >/dev/null 2>&1
}

install_packages() {
  step "安装语音识别库（Installing speech-recognition packages）…"
  "$PY" -m pip install --no-deps --no-input --no-cache-dir --disable-pip-version-check -r requirements.txt \
    || die "安装语音识别库失败，多半是网络问题。请检查网络后再运行一次：bash install.sh
  (pip install failed — check your network and run this script again.)"
  cp requirements.txt "$STAMP"
}

# ── 0. 检查这台电脑能不能用 ──────────────────────────────────────────────
[ -f requirements.txt ] && [ -d subtitle_tool ] \
  || die "请在“字幕翻译工具”的文件夹里运行这个脚本。(Run this script inside the tool's folder.)"

[ "$(uname -s)" = "Darwin" ] \
  || die "这个工具只能在 Mac 上运行。(This tool only runs on macOS.)"

[ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = "1" ] \
  || die "需要 Apple 芯片（M1 / M2 / M3 / M4 …）的 Mac；Intel 芯片的 Mac 用不了。
  (Apple Silicon is required. Intel Macs are not supported.)"

OS_VERSION="$(sw_vers -productVersion)"
[ "${OS_VERSION%%.*}" -ge 14 ] \
  || die "需要 macOS 14 (Sonoma) 或更新的系统，这台 Mac 是 macOS $OS_VERSION。请先升级系统。
  (macOS 14 or later is required; this Mac runs $OS_VERSION.)"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

# ── 1. 已经装好了？ ──────────────────────────────────────────────────────
if [ "$FORCE" = 0 ] && runtime_ok; then
  if cmp -s requirements.txt "$STAMP"; then
    ok "运行环境已经装好，不需要再安装。(Already installed.)"
  else
    install_packages   # 工具更新后依赖有变化：只补装变化的部分
    runtime_ok || die "更新后运行环境不完整，请运行：bash install.sh --force"
    ok "运行环境已更新。(Runtime updated.)"
  fi
  exit 0
fi

FREE_KB="$(df -Pk . | awk 'NR==2 {print $4}')"
[ "${FREE_KB:-0}" -ge $((3 * 1024 * 1024)) ] \
  || die "磁盘空间不够：安装需要约 3 GB 空闲空间（之后第一次使用还要下载 1.6 GB 的语音识别模型）。
  (Not enough disk space: about 3 GB free is needed.)"

echo
echo "  即将安装运行环境：约 1 GB，需要联网，一般 5～10 分钟。"
echo "  全部装在本文件夹的 runtime/ 里，不需要密码，不影响系统里的其它软件。"
echo "  (Installing the runtime into ./runtime — about 1 GB, 5–10 minutes.)"

if [ -e runtime ]; then
  step "清理不完整的旧运行环境（Removing the old runtime）…"
  rm -rf runtime
fi

# ── 2. 下载安装器 micromamba（一个 10 MB 左右的小程序，装完就删）────────────
step "下载安装器（Downloading the installer, micromamba）…"
rm -rf "$TMP"
mkdir -p "$TMP"
curl -fL --retry 3 --connect-timeout 20 --progress-bar "$MICROMAMBA_URL" -o "$TMP/micromamba.tar.bz2" \
  || die "下载安装器失败，请检查网络后再运行一次：bash install.sh
  (Download failed — check your network and run this script again.)"
tar -xjf "$TMP/micromamba.tar.bz2" -C "$TMP" bin/micromamba \
  || die "安装器文件损坏，请再运行一次：bash install.sh"

# ── 3. Python + ffmpeg ───────────────────────────────────────────────────
step "安装 Python 和 ffmpeg（约 600 MB，这一步最久）…"
# 安装器运行时把 HOME 临时指到 .install-tmp：这样它不会在你的个人文件夹里留下任何记录或缓存
mkdir -p "$TMP/home"
HOME="$TMP/home" "$TMP/bin/micromamba" create --yes --no-rc \
    --root-prefix "$TMP/root" --prefix "$PWD/runtime" \
    --channel "$CHANNEL" --override-channels --platform osx-arm64 \
    python=3.12 pip ffmpeg \
  || die "安装 Python / ffmpeg 失败，多半是网络问题。请检查网络后再运行一次：bash install.sh
  (Installing Python / ffmpeg failed — check your network and run this script again.)"

# ── 4. 语音识别库 ────────────────────────────────────────────────────────
install_packages

# ── 5. 检查一遍 ──────────────────────────────────────────────────────────
step "检查安装结果（Verifying）…"
runtime_ok || die "安装完成了，但检查没有通过。请运行：bash install.sh --force
  如果还是不行，请把上面的全部文字复制下来，到项目的 GitHub 页面提一个 Issue。
  (Verification failed. Try: bash install.sh --force)"

chmod +x "字幕翻译工具.command" 2>/dev/null || true

echo
ok "安装完成！(Installation complete!)"
echo
echo "  启动方法：在访达 (Finder) 里双击本文件夹中的「字幕翻译工具.command」"
echo "  或者在终端里运行：  \"$PWD/字幕翻译工具.command\""
echo "  (To start: double-click 字幕翻译工具.command in this folder.)"
echo

# 手动运行本脚本时，顺手在访达里把启动文件指给用户看
if [ -z "${SUBTITLE_TOOL_NO_REVEAL:-}" ] && [ -t 1 ]; then
  open -R "字幕翻译工具.command" 2>/dev/null || true
fi
