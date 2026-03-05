"""
Manastone Diagnostic Web UI - Gradio 界面
"""

import asyncio
import importlib.util
import logging
import os
import threading


def _normalize_proxy_env() -> None:
    """兼容部分环境里的 socks:// 代理写法（httpx 需要 socks5://）。"""
    proxy_vars = (
        "ALL_PROXY", "all_proxy",
        "HTTP_PROXY", "http_proxy",
        "HTTPS_PROXY", "https_proxy",
    )
    for key in proxy_vars:
        value = os.getenv(key)
        if value and value.startswith("socks://"):
            fixed = "socks5://" + value[len("socks://"):]
            os.environ[key] = fixed
            logging.getLogger(__name__).warning(
                "环境变量 %s 使用了 socks://，已自动改为 %s", key, fixed
            )

    # 如果仍使用 SOCKS 代理但环境未安装 socksio，则禁用代理避免 Gradio/httpx 启动失败
    has_socks_proxy = any(
        (os.getenv(k) or "").startswith("socks5://")
        for k in proxy_vars
    )
    if has_socks_proxy and importlib.util.find_spec("socksio") is None:
        for key in proxy_vars:
            if os.getenv(key):
                os.environ.pop(key, None)
        logging.getLogger(__name__).warning(
            "检测到 SOCKS 代理但缺少 socksio，已为当前 manastone-ui 进程禁用代理环境变量。"
        )


_normalize_proxy_env()

import gradio as gr

from .config import get_config
from .dds_bridge import DDSBridge
from .dds_bridge.mock_scenarios import ScenarioType, SCENARIO_DESCRIPTIONS
from .llm import LLMClient
from .orchestrator import DiagnosticOrchestrator
from .resources.joints import JointsResource

logger = logging.getLogger(__name__)

# 全局状态
_dds_bridge: DDSBridge | None = None
_joints_resource: JointsResource | None = None
_orchestrator: DiagnosticOrchestrator | None = None
_dds_loop: asyncio.AbstractEventLoop | None = None


def _run_async(coro, timeout: float = 30.0):
    """在 DDS 事件循环中运行协程，同步等待结果（线程安全）"""
    if _dds_loop is None:
        raise RuntimeError("DDS 事件循环未初始化")
    future = asyncio.run_coroutine_threadsafe(coro, _dds_loop)
    return future.result(timeout=timeout)


def start_dds_thread() -> None:
    """启动 DDS + LLM + Orchestrator 后台线程"""
    global _dds_loop
    config = get_config()
    _dds_loop = asyncio.new_event_loop()
    ready = threading.Event()

    async def _startup():
        global _dds_bridge, _joints_resource, _orchestrator
        _dds_bridge = DDSBridge()
        await _dds_bridge.start()
        _joints_resource = JointsResource(_dds_bridge)
        llm = LLMClient(config.llm)
        _orchestrator = DiagnosticOrchestrator(llm, config.knowledge_dir)
        if config.llm.use_remote:
            logger.info(f"LLM: 远端 {config.llm.remote_model} @ {config.llm.remote_url}")
        else:
            logger.info(f"LLM: 本地 {config.llm.local_model} @ {config.llm.local_url}")
        ready.set()

    def _run():
        asyncio.set_event_loop(_dds_loop)
        _dds_loop.run_until_complete(_startup())
        _dds_loop.run_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    ready.wait(timeout=10)


def stop_dds_thread() -> None:
    if _dds_bridge and _dds_loop:
        asyncio.run_coroutine_threadsafe(_dds_bridge.stop(), _dds_loop).result(timeout=5)
    if _dds_loop:
        _dds_loop.call_soon_threadsafe(_dds_loop.stop)


# ---------- 同步数据访问函数（供 Gradio 回调使用） ----------

def get_joint_status() -> dict:
    try:
        return _run_async(_joints_resource.get_status())
    except Exception as e:
        return {"error": str(e)}


def diagnose(focus: str = "all") -> str:
    try:
        data = _run_async(_joints_resource.get_status())
        if data.get("status") == "unavailable":
            return "暂无机器人数据"

        lines = ["## 诊断结果", ""]
        lines.append(f"**关节总数**: {data.get('joint_count', 0)}")
        lines.append(f"**异常数量**: {data.get('anomaly_count', 0)}")
        lines.append("")

        anomalies = data.get("anomalies", [])
        if anomalies:
            lines.append("### 检测到的问题")
            for a in anomalies:
                icon = "🔴" if a.get("level") == "critical" else "🟡"
                lines.append(f"{icon} **{a.get('joint_name')}**: {a.get('value', 0):.1f}°C")
        else:
            lines.append("✅ 未发现明显异常")

        return "\n".join(lines)
    except Exception as e:
        return f"诊断失败: {e}"


def compare_symmetric() -> str:
    try:
        data = _run_async(_joints_resource.compare_symmetric())
        if data.get("status") != "ok":
            return "暂无数据"

        lines = ["## 左右关节对比", ""]
        for comp in data.get("comparisons", []):
            alert = f" ⚠️ {comp['alert']}" if "alert" in comp else ""
            lines.append(
                f"**{comp['joint_pair']}**: "
                f"温差 {comp['temperature_diff']:.1f}°C | "
                f"扭矩差 {comp['torque_diff']:.1f}Nm{alert}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"对比失败: {e}"


def chat_response(message: str, history: list) -> tuple[str, list]:
    """通过 LLM + 知识库回答问题"""
    history = history or []
    if not message.strip():
        return "", history

    try:
        # 获取当前机器人状态
        joints_status = _run_async(_joints_resource.get_status())
        # 编排器处理（LLM + 知识库）
        reply = _run_async(
            _orchestrator.handle_query(message, joints_status),
            timeout=60.0,
        )
    except Exception as e:
        reply = f"处理失败: {e}"

    history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": reply},
    ]
    return "", history


def switch_scenario(scenario_value: str) -> str:
    """切换 mock 场景"""
    if _dds_bridge is None:
        return "DDS 未初始化"
    ok = _dds_bridge.set_scenario(scenario_value)
    if ok:
        desc = SCENARIO_DESCRIPTIONS.get(ScenarioType(scenario_value), scenario_value)
        return f"已切换到：{desc}"
    return "场景切换失败（仅 Mock 模式有效）"


def get_current_scenario() -> str:
    """获取当前场景名"""
    if _dds_bridge is None:
        return "未知"
    s = _dds_bridge.get_scenario()
    if s is None:
        return "真实 DDS 模式（无场景）"
    desc = SCENARIO_DESCRIPTIONS.get(ScenarioType(s), s)
    return f"当前场景: {desc}"


# ---------- UI ----------

def create_ui() -> gr.Blocks:
    config = get_config()
    llm_label = (
        f"远端 {config.llm.remote_model}" if config.llm.use_remote
        else f"本地 {config.llm.local_model}"
    )

    # 场景选项列表
    scenario_choices = [
        (desc, st.value)
        for st, desc in SCENARIO_DESCRIPTIONS.items()
    ]

    with gr.Blocks(title="Manastone Diagnostic - G1 运维诊断") as ui:
        gr.Markdown(f"""
        # 🔧 Manastone Diagnostic
        **G1 语义化运维诊断工具** | LLM: `{llm_label}`
        """)

        with gr.Tab("💬 智能诊断"):
            chatbot = gr.Chatbot(height=480)
            with gr.Row():
                msg_input = gr.Textbox(
                    label="描述问题",
                    placeholder="例如：左腿发烫 / 走路往右偏 / 关节卡顿",
                    lines=1,
                    scale=9,
                )
                send_btn = gr.Button("发送", scale=1, variant="primary")
            msg_input.submit(chat_response, [msg_input, chatbot], [msg_input, chatbot])
            send_btn.click(chat_response, [msg_input, chatbot], [msg_input, chatbot])
            gr.Examples(
                examples=[
                    "左腿发烫，什么情况？",
                    "走路一直往右偏",
                    "关节过热怎么处理？",
                    "摄像头无法初始化",
                    "IMU 数据漂移严重",
                ],
                inputs=msg_input,
            )

        with gr.Tab("📊 实时状态"):
            refresh_btn = gr.Button("🔄 刷新数据")
            status_json = gr.JSON(label="关节状态")
            refresh_btn.click(get_joint_status, outputs=status_json)

        with gr.Tab("🔍 快速诊断"):
            with gr.Row():
                diag_btn_temp = gr.Button("🌡️ 温度诊断")
                diag_btn_sym = gr.Button("⚖️ 左右对比")
                diag_btn_all = gr.Button("🔎 全面诊断")
            diag_output = gr.Markdown()
            diag_btn_temp.click(lambda: diagnose("temperature"), outputs=diag_output)
            diag_btn_sym.click(compare_symmetric, outputs=diag_output)
            diag_btn_all.click(lambda: diagnose("all"), outputs=diag_output)

        with gr.Tab("🎮 场景模拟"):
            gr.Markdown("""
            ### Mock 故障场景切换
            选择一个模拟场景，机器人数据将实时切换到对应故障状态。
            温度/扭矩遵循物理模型（一阶热模型 + 步态仿真），渐进式变化。
            """)
            current_scenario_label = gr.Markdown(
                value=get_current_scenario,
                every=3,
            )
            scenario_radio = gr.Radio(
                choices=scenario_choices,
                label="选择场景",
                value=ScenarioType.NORMAL_WALKING.value,
            )
            switch_btn = gr.Button("▶ 切换场景", variant="primary")
            switch_result = gr.Markdown()

            switch_btn.click(
                switch_scenario,
                inputs=scenario_radio,
                outputs=switch_result,
            )

            gr.Markdown("""
            ---
            **场景说明：**
            | 场景 | 描述 |
            |------|------|
            | 正常站立 | 低负载待机，关节温度约 30-35°C |
            | 正常行走 | 步态周期运动，温度缓慢上升至 40-45°C |
            | 左膝过热 | 左膝持续高负载，温度超过 70°C 阈值 |
            | 双膝过热 | 双侧膝关节长时间过热，超过警戒线 |
            | 编码器故障 | 左髋位置反馈出现随机跳变噪声 |
            | 左右不对称 | 右腿代偿，左右温差 > 10°C |
            | 持重物 | 双臂和腰部高扭矩，上肢温度上升 |
            | 右踝发僵 | 踝关节阻力大，高扭矩低速度 |
            """)

        gr.Markdown("---\n**Manastone Diagnostic v0.1** | G1 Orin NX | 完全离线可用")

    return ui


def main():
    config = get_config()

    start_dds_thread()
    logger.info("DDS + Orchestrator 已启动")

    try:
        ui = create_ui()
        logger.info(f"🌐 Web UI 启动于 http://{config.ui.host}:{config.ui.port}")
        ui.launch(
            server_name=config.ui.host,
            server_port=config.ui.port,
            share=config.ui.share,
            show_error=True,
        )
    finally:
        stop_dds_thread()


if __name__ == "__main__":
    main()
