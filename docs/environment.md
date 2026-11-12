# 环境、安装与维护

## 已验证环境

Windows x64，Python 3.12.10，NT 1.231.0。当前 Python 包约束为 `>=3.12,<3.13`，固定送转二进制仅支持 Windows x64。

```powershell
python -m pip install -e ".[test]" -c requirements-tested.txt
python -m pytest -q
```

`requirements-tested.txt` 记录本轮实测的直接依赖版本，便于复验；完整长期归档应同时保留解析后的全部依赖和安装包。运行时不需要 Tushare 网络 SDK，也不需要 Web 服务依赖；本地数据接入通过 PyArrow 只读完成。

## 送转桥接与 ABI

`engine/split_native.pyx` 是 NT 1.231.0 的固定 Cython ABI 桥，配套 `.pyd` 与 `SPLIT_BUILD.json`。`load_bridge()` 在加载前核对 Python/平台/NT 版本，以及源码、桥接二进制、NT `position` 二进制和声明文件的 SHA-256。文本按统一换行校验。

版本漂移时检查失败。升级 NT、Python 主次版本或目标平台时，需要重新审核桥接字段、构建二进制、更新指纹，并运行全部送转与账户守恒测试。当前仓库发布预构建桥；未提供跨平台自动重建工具链。

## 打包

```powershell
python -m pip wheel --no-deps --wheel-dir dist .
```

wheel 显式包含 `engine`、`contracts` 和送转的 `.pyd/.pyx/JSON` 资源，使用平台标签 `cp312-cp312-win_amd64`。安装后的 `engine.split_guard.load_bridge()` 是资源与 ABI 验证入口。

## 持续集成

`.github/workflows/tests.yml` 在 Windows/Python 3.12.10 上安装已测依赖，执行全套测试、CLI 样例、wheel 构建，并在仓库外验证已安装桥接资源。本地 Tushare 原始数据与凭据不上传到 CI；真实数据回放通过本地命令独立执行。
