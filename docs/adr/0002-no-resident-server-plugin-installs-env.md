# 不用常驻 server，由插件首次使用时用 uv 安装翻译环境

zotero-pdf2zh 依赖用户在终端手动启动的本机 Flask server（127.0.0.1:8890）来建环境和跑翻译。本插件直接以子进程启动 Python，不需要 server；为做到即插即用，首次翻译时插件自行下载 uv，在 `%LOCALAPPDATA%\babeldoc-for-zotero` 建 Python 3.12 环境并安装固定版本的 BabelDOC 和资源，默认走国内镜像。不再提供手动填写 BabelDOC 路径的选项，以保证版本可控。
