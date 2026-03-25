"""
manastone-pid-tuner
PID 自动调参 MCP Server

工具列表：
  pid_safety_check       - 调参前安全检查（温度、电量、通信）
  pid_run_experiment     - 执行单次阶跃响应实验，返回评分和诊断
  pid_propose_params     - LLM 驱动的下一组参数建议（含规则兜底）
  pid_run_auto_tuning    - [Python 循环] 全自动调参（LLM 作为子函数调用）
  pid_run_research_loop  - [Agent 循环] AutoResearch 风格，LLM 控制外层迭代
  pid_get_history        - 查看某关节的调参历史
  pid_clear_history      - 清空某关节的调参历史（重新开始）
  pid_get_best           - 获取历史最优参数

两种自动调参模式的架构区别：
  pid_run_auto_tuning   → Python for-loop 控制迭代，每轮调 LLM 一次作为子函数
                          LLM 只负责建议参数，Python 决定何时停止
  pid_run_research_loop → LLM while-loop 控制迭代（AutoResearch 风格）
                          LLM 通过 tool_calls 决定每一步：跑实验/查历史/结束
                          Python 只是工具执行者，不控制迭代逻辑
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import AsyncIterator, Optional

from mcp.server.fastmcp import FastMCP, Context

from .base import AppState, init_shared_state, shutdown_shared_state, get_shared_state
from ..pid_tuning.safety import SafetyGuard
from ..pid_tuning.experiment import ExperimentRunner, ExperimentConfig
from ..pid_tuning.optimizer import TuningHistory, PIDOptimizer
from ..pid_tuning.agent_loop import PIDAgentLoop
from ..motion import ScenarioLibrary

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(server: FastMCP, **kwargs) -> AsyncIterator[AppState]:
    state = await init_shared_state(**kwargs)
    logger.info("manastone-pid-tuner ready (mock_mode=%s)", state.mock_mode)
    try:
        yield state
    finally:
        await shutdown_shared_state()


def create_server(**init_kwargs) -> FastMCP:
    mcp = FastMCP(
        "manastone-pid-tuner",
        lifespan=partial(_lifespan, **init_kwargs),
    )

    # ── 内部辅助：从 AppState 懒加载子系统 ────────────────────
    def _get_subsystems():
        s = get_shared_state()

        # 从 schema 读取 PID 安全边界配置（如果有）
        pid_bounds_config = getattr(s.schema, "pid_safety_bounds", None) or {}
        safety = SafetyGuard(schema_pid_bounds=pid_bounds_config)

        storage_dir = Path(
            init_kwargs.get("storage_dir", "storage")
        )
        history = TuningHistory(storage_dir)
        llm_client = getattr(s, "llm_client", None)
        optimizer = PIDOptimizer(history=history, llm_client=llm_client)
        runner = ExperimentRunner(
            safety_guard=safety,
            mock_mode=s.mock_mode,
            dds_bridge=s.dds_bridge,
        )
        return s, safety, runner, history, optimizer

    def _get_joint_info(s: AppState, joint_name: str) -> Optional[dict]:
        """从 schema 查找关节的 motor_index 和 group"""
        for topic in s.schema.topics:
            for idx, info in topic.motor_index_map.items():
                if info.get("name") == joint_name:
                    return {"index": idx, "group": info.get("group", "default"), **info}
        return None

    async def _get_env_snapshot(s: AppState, joint_name: str) -> dict:
        """采集当前环境快照（用于一致性比对）"""
        snapshot = {"timestamp": time.time(), "joint_name": joint_name}

        try:
            # 电量
            raw = await s.dds_bridge.get_topic_data("/lf/lowstate")
            if raw:
                snapshot["battery_soc_pct"] = raw.get("bms_state", {}).get("soc", -1)
                snapshot["battery_v"] = raw.get("power_v", -1)

                # 关节温度
                joint_info = _get_joint_info(s, joint_name)
                if joint_info:
                    idx = joint_info["index"]
                    motor_states = raw.get("motor_state", [])
                    for ms in motor_states:
                        if ms.get("motor_index") == idx:
                            snapshot["joint_temp_c"] = ms.get("temperature", -1)
                            snapshot["comm_lost"] = ms.get("lost", 0)
                            break
        except Exception as e:
            logger.debug("环境快照采集异常: %s", e)

        return snapshot

    # ════════════════════════════════════════════════════════════
    # Tool 1: 安全检查
    # ════════════════════════════════════════════════════════════
    @mcp.tool()
    async def pid_safety_check(
        joint_name: str,
        ctx: Context = None,
    ) -> str:
        """
        调参前安全检查：读取当前关节温度、电量、通信状态。
        必须在 pid_run_experiment 之前调用，确认 passed=true 才能继续。

        Args:
            joint_name: 关节名，如 "left_knee"、"right_hip_pitch"
        """
        s, safety, runner, history, optimizer = _get_subsystems()

        joint_info = _get_joint_info(s, joint_name)
        if joint_info is None:
            return json.dumps({
                "error": f"未找到关节 '{joint_name}'，请用 joint_schema 工具查看可用关节"
            }, ensure_ascii=False)

        group = joint_info.get("group", "default")
        bounds = safety.get_bounds(joint_name, group)

        # 采集当前状态
        snapshot = await _get_env_snapshot(s, joint_name)
        temp = snapshot.get("joint_temp_c", 25.0)
        soc = snapshot.get("battery_soc_pct", 100.0)
        comm_lost = snapshot.get("comm_lost", 0)

        pre_check = safety.pre_experiment_check(
            joint_name=joint_name,
            current_temp_c=temp,
            battery_soc_pct=soc,
            comm_lost=comm_lost,
            joint_group=group,
        )

        return json.dumps({
            "joint_name": joint_name,
            "joint_group": group,
            "environment": {
                "joint_temp_c": temp,
                "battery_soc_pct": soc,
                "comm_lost": comm_lost,
            },
            "safety_check": pre_check.to_dict(),
            "pid_bounds": {
                "kp": [bounds.kp_min, bounds.kp_max],
                "ki": [bounds.ki_min, bounds.ki_max],
                "kd": [bounds.kd_min, bounds.kd_max],
                "max_torque_nm": bounds.max_torque_nm,
                "max_velocity_rad_s": bounds.max_velocity_rad_s,
            },
            "ready_to_tune": pre_check.passed,
        }, ensure_ascii=False, indent=2)

    # ════════════════════════════════════════════════════════════
    # Tool 2: 单次实验
    # ════════════════════════════════════════════════════════════
    @mcp.tool()
    async def pid_run_experiment(
        joint_name: str,
        kp: float,
        ki: float,
        kd: float,
        setpoint_rad: float = 0.5,
        duration_s: float = 2.0,
        ctx: Context = None,
    ) -> str:
        """
        执行一次 PID 阶跃响应实验并返回量化评分。

        完整返回：
          score (0-100)、grade (A-F)、各维度指标、文字诊断、安全状态

        Args:
            joint_name:   目标关节名
            kp:           比例增益
            ki:           积分增益
            kd:           微分增益
            setpoint_rad: 阶跃目标位置（弧度），默认 0.5 rad ≈ 28.6°
            duration_s:   实验时长（秒），建议 1.5-3.0
        """
        s, safety, runner, history, optimizer = _get_subsystems()

        joint_info = _get_joint_info(s, joint_name)
        if joint_info is None:
            return json.dumps({"error": f"未找到关节 '{joint_name}'"}, ensure_ascii=False)
        group = joint_info.get("group", "default")

        # 1. 参数安全检查
        param_check = safety.check_pid_params(joint_name, kp, ki, kd, group)
        if not param_check.passed:
            return json.dumps({
                "error": "参数安全检查未通过，实验被拒绝",
                "violations": param_check.violations,
                "warnings": param_check.warnings,
            }, ensure_ascii=False, indent=2)

        # 2. 采集环境快照（用于一致性记录）
        snapshot = await _get_env_snapshot(s, joint_name)

        # 3. 执行实验
        config = ExperimentConfig(
            joint_name=joint_name,
            joint_group=group,
            kp=kp, ki=ki, kd=kd,
            setpoint_rad=setpoint_rad,
            duration_s=duration_s,
            mock_mode=s.mock_mode,
        )
        result = await runner.run(config, env_snapshot=snapshot)

        # 4. 持久化到历史
        history_entry = {
            "experiment_id": result.experiment_id,
            "timestamp": result.timestamp,
            "kp": kp, "ki": ki, "kd": kd,
            "score": result.metrics.score,
            "grade": result.metrics.grade,
            "overshoot_pct": result.metrics.overshoot_pct,
            "rise_time_s": result.metrics.rise_time_s,
            "settling_time_s": result.metrics.settling_time_s,
            "sse_pct": result.metrics.sse_pct,
            "oscillation_count": result.metrics.oscillation_count,
            "diagnosis": result.metrics.diagnosis,
            "safety_aborted": result.safety_aborted,
            "env_snapshot": snapshot,
        }
        history.save(joint_name, history_entry)

        # 5. 构建返回
        resp = result.to_dict(include_raw=False)
        resp["param_warnings"] = param_check.warnings
        return json.dumps(resp, ensure_ascii=False, indent=2)

    # ════════════════════════════════════════════════════════════
    # Tool 3: LLM 驱动的参数建议
    # ════════════════════════════════════════════════════════════
    @mcp.tool()
    async def pid_propose_params(
        joint_name: str,
        current_kp: float = 5.0,
        current_ki: float = 0.1,
        current_kd: float = 0.5,
        current_score: float = 0.0,
        ctx: Context = None,
    ) -> str:
        """
        基于历史数据，用 LLM（或规则）建议下一组 Kp/Ki/Kd 参数。

        这是"决策层"工具，实现 AutoResearch 中的"分析→假设"环节。
        LLM 会阅读历史波形诊断，提出调整依据，不只是改数字。

        Args:
            joint_name:    关节名
            current_kp/ki/kd: 当前参数（作为上下文）
            current_score: 当前得分
        """
        s, safety, runner, history, optimizer = _get_subsystems()

        joint_info = _get_joint_info(s, joint_name)
        if joint_info is None:
            return json.dumps({"error": f"未找到关节 '{joint_name}'"}, ensure_ascii=False)
        group = joint_info.get("group", "default")
        bounds = safety.get_bounds(joint_name, group)

        params, reasoning = await optimizer.propose_next(
            joint_name=joint_name,
            joint_group=group,
            current_params={"kp": current_kp, "ki": current_ki, "kd": current_kd},
            current_score=current_score,
            bounds=bounds,
        )

        # 验证提议参数合法性
        check = safety.check_pid_params(joint_name, params["kp"], params["ki"], params["kd"], group)

        return json.dumps({
            "joint_name": joint_name,
            "proposed_params": params,
            "reasoning": reasoning,
            "safety_check": check.to_dict(),
            "llm_used": bool(
                getattr(s, "llm_client", None) and
                getattr(s, "llm_client", None).is_available()
            ),
        }, ensure_ascii=False, indent=2)

    # ════════════════════════════════════════════════════════════
    # Tool 4: 全自动调参闭环
    # ════════════════════════════════════════════════════════════
    @mcp.tool()
    async def pid_run_auto_tuning(
        joint_name: str,
        max_iterations: int = 20,
        target_score: float = 85.0,
        setpoint_rad: float = 0.5,
        experiment_duration_s: float = 2.0,
        ctx: Context = None,
    ) -> str:
        """
        全自动 PID 调参闭环（AutoResearch 风格）。

        完整闭环：
          [安全检查] → [提出假设] → [执行实验] → [评分] → [迭代]

        每次迭代：
          1. 由 LLM 或规则提出下一组参数
          2. 通过安全围栏校验
          3. 运行阶跃响应实验
          4. 评分；若达到 target_score 提前停止
          5. 将结果存入历史供下一轮 LLM 参考

        Args:
            joint_name:           目标关节名
            max_iterations:       最大迭代次数（默认 20）
            target_score:         目标分数达到后提前停止（默认 85）
            setpoint_rad:         阶跃目标位置
            experiment_duration_s: 每次实验时长
        """
        s, safety, runner, history, optimizer = _get_subsystems()

        joint_info = _get_joint_info(s, joint_name)
        if joint_info is None:
            return json.dumps({"error": f"未找到关节 '{joint_name}'"}, ensure_ascii=False)
        group = joint_info.get("group", "default")
        bounds = safety.get_bounds(joint_name, group)

        # 调参前环境检查
        snapshot = await _get_env_snapshot(s, joint_name)
        pre_check = safety.pre_experiment_check(
            joint_name=joint_name,
            current_temp_c=snapshot.get("joint_temp_c", 25.0),
            battery_soc_pct=snapshot.get("battery_soc_pct", 100.0),
            comm_lost=snapshot.get("comm_lost", 0),
            joint_group=group,
        )
        if not pre_check.passed:
            return json.dumps({
                "error": "调参前安全检查未通过",
                "violations": pre_check.violations,
            }, ensure_ascii=False, indent=2)

        session_log = []
        best_score = 0.0
        best_params = {"kp": bounds.kp_min * 5, "ki": 0.0, "kd": bounds.kd_min}
        current_params = best_params.copy()
        current_score = 0.0

        start_time = time.time()

        for iteration in range(1, max_iterations + 1):
            iter_log = {"iteration": iteration}

            # 1. 提出下一组参数
            proposed, reasoning = await optimizer.propose_next(
                joint_name=joint_name,
                joint_group=group,
                current_params=current_params,
                current_score=current_score,
                bounds=bounds,
            )
            iter_log["proposed_params"] = proposed
            iter_log["reasoning"] = reasoning

            # 2. 安全校验
            check = safety.check_pid_params(
                joint_name, proposed["kp"], proposed["ki"], proposed["kd"], group
            )
            if not check.passed:
                iter_log["skipped"] = f"参数不安全: {check.violations}"
                session_log.append(iter_log)
                continue

            # 3. 执行实验
            config = ExperimentConfig(
                joint_name=joint_name,
                joint_group=group,
                kp=proposed["kp"],
                ki=proposed["ki"],
                kd=proposed["kd"],
                setpoint_rad=setpoint_rad,
                duration_s=experiment_duration_s,
                mock_mode=s.mock_mode,
            )
            result = await runner.run(config, env_snapshot=snapshot)

            # 4. 保存历史
            history_entry = {
                "experiment_id": result.experiment_id,
                "timestamp": result.timestamp,
                "kp": proposed["kp"], "ki": proposed["ki"], "kd": proposed["kd"],
                "score": result.metrics.score,
                "grade": result.metrics.grade,
                "overshoot_pct": result.metrics.overshoot_pct,
                "rise_time_s": result.metrics.rise_time_s,
                "settling_time_s": result.metrics.settling_time_s,
                "sse_pct": result.metrics.sse_pct,
                "oscillation_count": result.metrics.oscillation_count,
                "diagnosis": result.metrics.diagnosis,
                "safety_aborted": result.safety_aborted,
                "env_snapshot": snapshot,
            }
            history.save(joint_name, history_entry)

            iter_log["score"] = result.metrics.score
            iter_log["grade"] = result.metrics.grade
            iter_log["diagnosis"] = result.metrics.diagnosis
            iter_log["safety_aborted"] = result.safety_aborted

            # 5. 更新最优
            if result.metrics.score > best_score:
                best_score = result.metrics.score
                best_params = proposed.copy()

            current_params = proposed
            current_score = result.metrics.score

            session_log.append(iter_log)

            # 6. 检查停止条件
            if current_score >= target_score:
                break

        elapsed = time.time() - start_time
        return json.dumps({
            "joint_name": joint_name,
            "total_iterations": len([x for x in session_log if "score" in x]),
            "elapsed_s": round(elapsed, 1),
            "target_score": target_score,
            "target_reached": best_score >= target_score,
            "best_score": round(best_score, 1),
            "best_params": best_params,
            "session_log": session_log,
            "recommendation": (
                f"推荐参数：Kp={best_params['kp']:.3f} Ki={best_params['ki']:.4f} "
                f"Kd={best_params['kd']:.3f}，综合得分 {best_score:.1f}/100"
            ),
        }, ensure_ascii=False, indent=2)

    # ════════════════════════════════════════════════════════════
    # Tool 5: autoresearch 风格 Agent 循环（忠实复现 Karpathy 架构）
    # ════════════════════════════════════════════════════════════
    @mcp.tool()
    async def pid_run_research_loop(
        joint_name: str,
        target_score: float = 85.0,
        max_experiments: int = 50,
        initial_kp: float = 10.0,
        initial_ki: float = 0.1,
        initial_kd: float = 2.0,
        setpoint_rad: float = 0.5,
        experiment_duration_s: float = 2.0,
        ctx: Context = None,
    ) -> str:
        """
        Karpathy autoresearch 风格的 PID 调参循环。

        ┌ 架构对比（与 pid_run_auto_tuning）────────────────────┐
        │                                                        │
        │  pid_run_auto_tuning：                                 │
        │    LLM.chat(history) → 返回 kp/ki/kd 数字             │
        │    Python 用数字跑实验                                  │
        │    LLM 推理过程隐藏在内部                               │
        │                                                        │
        │  pid_run_research_loop（autoresearch 风格）：           │
        │    LLM 读取 params.yaml 文件（含假设注释）              │
        │    LLM 输出：修改后的 params.yaml 全文                  │
        │    Python 写文件 → git commit → 跑实验 → 评分          │
        │    改善 → git keep，未改善 → git revert params.yaml    │
        │    结果追加到 results.tsv（commit_hash, score, status） │
        │    直到达标或 max_experiments 安全网触发                 │
        └────────────────────────────────────────────────────────┘

        文件结构（storage/pid_workspace/{joint_name}/）：
          params.yaml     ← LLM 每轮修改（等价于 autoresearch/train.py）
          program.md      ← 人写的研究方向（等价于 autoresearch/program.md）
          results.tsv     ← 实验日志，git 历史就是研究日志
          best_params.yaml ← 历史最优快照

        Args:
            joint_name:            目标关节名
            target_score:          目标分数，达到后停止（默认 85）
            max_experiments:       安全网上限（默认 50，相当于"人中断"的替代）
            initial_kp/ki/kd:      起始参数
            setpoint_rad:          阶跃目标位置
            experiment_duration_s: 每次实验时长
        """
        s, safety, runner, history, optimizer = _get_subsystems()

        if not (getattr(s, "llm_client", None) and s.llm_client.is_available()):
            return json.dumps({
                "error": (
                    "pid_run_research_loop 需要 LLM（chat 接口）。"
                    "请配置 OPENAI_API_KEY 或本地 Qwen 端点，"
                    "或改用 pid_run_auto_tuning（规则兜底）。"
                )
            }, ensure_ascii=False, indent=2)

        joint_info = _get_joint_info(s, joint_name)
        if joint_info is None:
            return json.dumps({"error": f"未找到关节 '{joint_name}'"}, ensure_ascii=False)
        group = joint_info.get("group", "default")
        bounds = safety.get_bounds(joint_name, group)

        # 实验前安全检查
        snapshot = await _get_env_snapshot(s, joint_name)
        pre_check = safety.pre_experiment_check(
            joint_name=joint_name,
            current_temp_c=snapshot.get("joint_temp_c", 25.0),
            battery_soc_pct=snapshot.get("battery_soc_pct", 100.0),
            comm_lost=snapshot.get("comm_lost", 0),
            joint_group=group,
        )
        if not pre_check.passed:
            return json.dumps({
                "error": "安全检查未通过",
                "violations": pre_check.violations,
            }, ensure_ascii=False, indent=2)

        storage_dir = Path(init_kwargs.get("storage_dir", "storage"))
        agent = PIDAgentLoop(
            llm_client=s.llm_client,
            runner=runner,
            safety=safety,
            storage_dir=storage_dir,
        )
        result = await agent.run(
            joint_name=joint_name,
            joint_group=group,
            target_score=target_score,
            max_experiments=max_experiments,
            bounds=bounds,
            initial_kp=initial_kp,
            initial_ki=initial_ki,
            initial_kd=initial_kd,
            setpoint_rad=setpoint_rad,
            experiment_duration_s=experiment_duration_s,
        )

        return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)

    # ════════════════════════════════════════════════════════════
    # Tool 6: 查询历史
    # ════════════════════════════════════════════════════════════
    @mcp.tool()
    async def pid_get_history(
        joint_name: str,
        limit: int = 20,
        ctx: Context = None,
    ) -> str:
        """
        查看某关节的调参历史记录（时间顺序，最新在后）。

        Args:
            joint_name: 关节名
            limit: 返回条数上限
        """
        s, safety, runner, history, optimizer = _get_subsystems()
        records = history.recent(joint_name, limit)
        best = history.best(joint_name)

        return json.dumps({
            "joint_name": joint_name,
            "total_experiments": len(history.load(joint_name)),
            "best": best,
            "recent": records,
        }, ensure_ascii=False, indent=2)

    # ════════════════════════════════════════════════════════════
    # Tool 7: 获取历史最优
    # ════════════════════════════════════════════════════════════
    @mcp.tool()
    async def pid_get_best(
        joint_name: str,
        ctx: Context = None,
    ) -> str:
        """
        返回历史中得分最高的 PID 参数组合。
        调参完成后，用这个工具获取最终推荐参数。

        Args:
            joint_name: 关节名
        """
        s, safety, runner, history, optimizer = _get_subsystems()
        best = history.best(joint_name)

        if not best:
            return json.dumps({
                "joint_name": joint_name,
                "message": f"关节 {joint_name} 尚无调参历史，请先运行 pid_run_experiment 或 pid_run_auto_tuning",
            }, ensure_ascii=False, indent=2)

        return json.dumps({
            "joint_name": joint_name,
            "best_params": {"kp": best["kp"], "ki": best["ki"], "kd": best["kd"]},
            "best_score": best["score"],
            "grade": best["grade"],
            "metrics_summary": {
                "overshoot_pct": best.get("overshoot_pct"),
                "rise_time_s": best.get("rise_time_s"),
                "settling_time_s": best.get("settling_time_s"),
                "sse_pct": best.get("sse_pct"),
            },
            "diagnosis": best.get("diagnosis", []),
            "experiment_id": best.get("experiment_id"),
        }, ensure_ascii=False, indent=2)

    # ════════════════════════════════════════════════════════════
    # Tool 8: 清空历史
    # ════════════════════════════════════════════════════════════
    @mcp.tool()
    async def pid_clear_history(
        joint_name: str,
        ctx: Context = None,
    ) -> str:
        """
        清空某关节的调参历史（重新开始）。
        用于更换控制器版本或物理改造后重新调参。

        Args:
            joint_name: 关节名
        """
        s, safety, runner, history, optimizer = _get_subsystems()
        deleted = history.clear(joint_name)
        return json.dumps({
            "joint_name": joint_name,
            "deleted_records": deleted,
            "message": f"已清空关节 {joint_name} 的 {deleted} 条历史记录",
        }, ensure_ascii=False, indent=2)

    # ════════════════════════════════════════════════════════════
    # Tool 9: 场景库 — 列出所有预置运动场景
    # ════════════════════════════════════════════════════════════
    @mcp.tool()
    async def pid_list_scenarios(
        robot_type: str = "",
        ctx: Context = None,
    ) -> str:
        """
        列出所有预置运动场景（用于 pid_run_scenario 的 scenario 参数）。

        场景库覆盖：站立/行走/楼梯/蹲起/抓取/搬运/四足步态/装配等。
        每个场景包含：名称、关键词、适用机器人、阶段数、建议目标分数。

        Args:
            robot_type: 按机器人类型过滤（如 "unitree_g1"），空=全部
        """
        lib = ScenarioLibrary()
        if robot_type:
            scenarios = lib.for_robot(robot_type)
        else:
            scenarios = lib.all()

        return json.dumps({
            "total": len(scenarios),
            "robot_type_filter": robot_type or "全部",
            "scenarios": [
                {
                    "scenario_id": s.scenario_id,
                    "name": s.name,
                    "description": s.description,
                    "robot_types": s.robot_types or ["通用"],
                    "phases": len(s.phases),
                    "target_score_hint": s.target_score_hint,
                    "keywords": s.keywords,
                }
                for s in scenarios
            ],
            "usage": (
                "使用 pid_run_scenario(scenario='<scenario_id>', joint_name='left_knee') "
                "或 pid_run_scenario(scenario='上楼梯时右膝响应') 来运行场景实验"
            ),
        }, ensure_ascii=False, indent=2)

    # ════════════════════════════════════════════════════════════
    # Tool 10: 场景实验 — 按场景 ID 执行多阶段 PID 测试
    # ════════════════════════════════════════════════════════════
    @mcp.tool()
    async def pid_run_scenario(
        scenario_id: str,
        joint_name: str = "",
        kp: float = 0.0,
        ki: float = 0.0,
        kd: float = 0.0,
        ctx: Context = None,
    ) -> str:
        """
        按预置场景的物理参数（角度/时长/负载上下文）执行多阶段 PID 实验。

        先用 pid_list_scenarios 查看可用场景，再用此工具执行。
        场景中编码了每个动作阶段的物理约束和调参提示（phase_notes），
        用于在 research 中复现特定运动负载条件。

        若 kp/ki/kd 全为 0，自动使用该关节的历史最优参数。

        Args:
            scenario_id: 场景 ID，如 "stair_ascent"（用 pid_list_scenarios 查看）
            joint_name:  覆盖场景默认关节，如 "right_knee"（可选）
            kp/ki/kd:    PID 参数（0=使用历史最优或保守初始值）
        """
        s, safety, runner, history, optimizer = _get_subsystems()

        # ── 1. 查找场景 ──────────────────────────────────────
        lib = ScenarioLibrary()
        motion_scenario = lib.get(scenario_id)
        if motion_scenario is None:
            available = [sc.scenario_id for sc in lib.for_robot(s.schema.robot_type)]
            return json.dumps({
                "error": f"未找到场景 '{scenario_id}'",
                "available_for_this_robot": available,
                "tip": "调用 pid_list_scenarios 查看完整场景列表",
            }, ensure_ascii=False, indent=2)

        if joint_name:
            motion_scenario = motion_scenario.for_joint(joint_name)

        # ── 2. 逐阶段执行 ────────────────────────────────────
        phase_results = []
        best_phase_score = 0.0

        for phase_idx, phase in enumerate(motion_scenario.phases):
            joint_info = _get_joint_info(s, phase.joint_name)
            if joint_info is None:
                phase_results.append({
                    "phase_label": phase.phase_label,
                    "joint_name": phase.joint_name,
                    "error": f"关节 '{phase.joint_name}' 在当前 schema 中不存在，跳过",
                })
                continue

            group = joint_info.get("group", "default")
            bounds = safety.get_bounds(phase.joint_name, group)

            # 确定 PID 参数
            if kp == 0.0 and ki == 0.0 and kd == 0.0:
                # 用历史最优，没有历史则用 schema 边界中点作为保守初始值
                best = history.best(phase.joint_name)
                if best:
                    use_kp = best["kp"]
                    use_ki = best["ki"]
                    use_kd = best["kd"]
                    params_source = f"历史最优（score={best['score']:.1f}）"
                else:
                    use_kp = (bounds.kp_min + bounds.kp_max) * 0.2
                    use_ki = 0.0
                    use_kd = (bounds.kd_min + bounds.kd_max) * 0.1
                    params_source = "默认初始值（无历史）"
            else:
                use_kp, use_ki, use_kd = kp, ki, kd
                params_source = "指定参数"

            # 参数安全检查
            param_check = safety.check_pid_params(phase.joint_name, use_kp, use_ki, use_kd, group)
            if not param_check.passed:
                phase_results.append({
                    "phase_label": phase.phase_label,
                    "joint_name": phase.joint_name,
                    "error": f"参数安全检查未通过: {param_check.violations}",
                })
                continue

            # 采集环境快照
            snapshot = await _get_env_snapshot(s, phase.joint_name)

            # 执行实验
            exp_config = ExperimentConfig(
                joint_name=phase.joint_name,
                joint_group=group,
                kp=use_kp,
                ki=use_ki,
                kd=use_kd,
                setpoint_rad=phase.setpoint_rad,
                duration_s=phase.duration_s,
                initial_position_rad=phase.initial_position_rad,
                mock_mode=s.mock_mode,
            )
            result = await runner.run(exp_config, env_snapshot=snapshot)

            # 保存到历史
            history.save(phase.joint_name, {
                "experiment_id": result.experiment_id,
                "timestamp": result.timestamp,
                "kp": use_kp, "ki": use_ki, "kd": use_kd,
                "score": result.metrics.score,
                "grade": result.metrics.grade,
                "overshoot_pct": result.metrics.overshoot_pct,
                "rise_time_s": result.metrics.rise_time_s,
                "settling_time_s": result.metrics.settling_time_s,
                "sse_pct": result.metrics.sse_pct,
                "oscillation_count": result.metrics.oscillation_count,
                "diagnosis": result.metrics.diagnosis,
                "safety_aborted": result.safety_aborted,
                "env_snapshot": snapshot,
                "scenario_id": motion_scenario.scenario_id,
                "phase_label": phase.phase_label,
            })

            if result.metrics.score > best_phase_score:
                best_phase_score = result.metrics.score

            phase_results.append({
                "phase_label": phase.phase_label,
                "phase_notes": phase.phase_notes,
                "joint_name": phase.joint_name,
                "setpoint_rad": phase.setpoint_rad,
                "params": {"kp": use_kp, "ki": use_ki, "kd": use_kd},
                "params_source": params_source,
                "score": result.metrics.score,
                "grade": result.metrics.grade,
                "overshoot_pct": result.metrics.overshoot_pct,
                "rise_time_s": result.metrics.rise_time_s,
                "settling_time_s": result.metrics.settling_time_s,
                "diagnosis": result.metrics.diagnosis,
                "safety_aborted": result.safety_aborted,
            })

        # ── 3. 汇总结果 ───────────────────────────────────────
        completed = [p for p in phase_results if "score" in p]
        avg_score = (
            sum(p["score"] for p in completed) / len(completed)
            if completed else 0.0
        )

        return json.dumps({
            "scenario": {
                "id": motion_scenario.scenario_id,
                "name": motion_scenario.name,
                "description": motion_scenario.description,
                "joint_override": joint_name or None,
            },
            "summary": {
                "total_phases": len(motion_scenario.phases),
                "completed_phases": len(completed),
                "avg_score": round(avg_score, 1),
                "target_score_hint": motion_scenario.target_score_hint,
                "suggestion": (
                    f"场景建议目标分数 {motion_scenario.target_score_hint}，当前均分 {avg_score:.1f}。"
                    + ("继续用 pid_run_auto_tuning 优化参数。" if avg_score < motion_scenario.target_score_hint else "已达到场景目标！")
                ),
            },
            "phases": phase_results,
        }, ensure_ascii=False, indent=2)

    return mcp


def main():
    """独立运行 pid_tuner server"""
    import asyncio
    import os
    from pathlib import Path

    init_kwargs = {
        "schema_path": Path(os.getenv("MANASTONE_SCHEMA_PATH", "config/robot_schema.yaml")),
        "storage_dir": Path(os.getenv("MANASTONE_STORAGE_DIR", "storage")),
        "robot_id": os.getenv("MANASTONE_ROBOT_ID", "robot_01"),
        "mock_mode": os.getenv("MANASTONE_MOCK_MODE", "true").lower() == "true",
    }
    mcp = create_server(**init_kwargs)
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = int(os.getenv("MANASTONE_PORT", "8087"))
    mcp.run(transport=os.getenv("MANASTONE_TRANSPORT", "sse"))
