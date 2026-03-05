"""
DDS Bridge - X2 ROS2 订阅和缓存模块
"""

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..config import get_config

logger = logging.getLogger(__name__)


@dataclass
class JointState:
    """关节状态数据"""
    joint_id: int
    position: float  # 弧度
    velocity: float  # 弧度/秒
    torque: float    # Nm
    temperature: float  # °C
    timestamp: float = field(default_factory=time.time)


@dataclass
class LowState:
    """统一内部 LowState 消息结构（兼容上层资源和诊断逻辑）"""
    level_flag: int = 0
    comm_version: int = 0
    robot_id: int = 0
    sn: List[int] = field(default_factory=lambda: [0]*2)
    bandwidth: int = 0
    motor_state: List[JointState] = field(default_factory=list)
    bms_state: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class DDSCache:
    """DDS 消息缓存 - 滑动窗口"""
    
    def __init__(self, max_size: int = 1000, window_seconds: int = 300):
        self.max_size = max_size
        self.window_seconds = window_seconds
        self._cache: deque = deque(maxlen=max_size)
        self._lock = asyncio.Lock()
    
    async def put(self, data: Any) -> None:
        """存入缓存"""
        async with self._lock:
            self._cache.append({
                "data": data,
                "timestamp": time.time()
            })
    
    async def get_recent(self, seconds: Optional[int] = None) -> List[Any]:
        """获取最近几秒的数据"""
        seconds = seconds or self.window_seconds
        cutoff = time.time() - seconds
        
        async with self._lock:
            return [
                item["data"] 
                for item in self._cache 
                if item["timestamp"] >= cutoff
            ]
    
    async def get_latest(self) -> Optional[Any]:
        """获取最新一条数据"""
        async with self._lock:
            if self._cache:
                return self._cache[-1]["data"]
            return None
    
    async def get_trend(self, field: str, seconds: int = 600) -> Dict[str, float]:
        """获取某字段的趋势数据"""
        recent = await self.get_recent(seconds)
        if not recent:
            return {"start": 0, "end": 0, "min": 0, "max": 0, "avg": 0}
        
        values = []
        for item in recent:
            if isinstance(item, dict) and field in item:
                values.append(item[field])
            elif hasattr(item, field):
                values.append(getattr(item, field))
        
        if not values:
            return {"start": 0, "end": 0, "min": 0, "max": 0, "avg": 0}
        
        return {
            "start": values[0],
            "end": values[-1],
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / len(values)
        }


class MockDDSSubscriber:
    """模拟 DDS 订阅器 —— 基于 ScenarioEngine 的物理仿真"""

    def __init__(self):
        from .mock_scenarios import ScenarioEngine, ScenarioType
        self.callbacks: Dict[str, List[Callable]] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._engine = ScenarioEngine()
        self.ScenarioType = ScenarioType

    @property
    def scenario(self):
        return self._engine.scenario

    @scenario.setter
    def scenario(self, s):
        self._engine.scenario = s
        logger.info(f"场景切换 → {s}")

    def register_callback(self, topic: str, callback: Callable) -> None:
        if topic not in self.callbacks:
            self.callbacks[topic] = []
        self.callbacks[topic].append(callback)
        logger.info(f"已注册回调: {topic}")

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"模拟 DDS 订阅器已启动（场景: {self._engine.scenario}）")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("模拟 DDS 订阅器已停止")

    async def _loop(self) -> None:
        while self._running:
            try:
                joint_dicts = self._engine.step()

                motor_state = [
                    JointState(
                        joint_id=j["joint_id"],
                        position=j["position"],
                        velocity=j["velocity"],
                        torque=j["torque"],
                        temperature=j["temperature"],
                    )
                    for j in joint_dicts
                ]

                lowstate = LowState(
                    level_flag=1,
                    comm_version=1,
                    robot_id=1,
                    motor_state=motor_state,
                    timestamp=time.time(),
                )

                topic = "rt/lf/lowstate"
                await _dispatch_callbacks(self.callbacks, topic, lowstate)

                await asyncio.sleep(0.5)  # 2 Hz

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"生成模拟数据错误: {e}")
                await asyncio.sleep(1)


class DDSSubscriber:
    """真实 ROS2 订阅器（读取 X2 话题）"""

    _TOPIC_TO_OFFSET = {
        "joint_leg_state": 0,
        "joint_waist_state": 12,
        "joint_arm_state": 14,
        "joint_head_state": 26,
    }

    _NAME_TO_JOINT_ID = {
        "l_hip_yaw": 0, "left_hip_yaw": 0, "l_hip_pitch": 1, "left_hip_pitch": 1,
        "l_hip_roll": 2, "left_hip_roll": 2, "l_knee": 3, "left_knee": 3,
        "l_ankle_pitch": 4, "left_ankle_pitch": 4, "l_ankle_roll": 5, "left_ankle_roll": 5,
        "r_hip_yaw": 6, "right_hip_yaw": 6, "r_hip_pitch": 7, "right_hip_pitch": 7,
        "r_hip_roll": 8, "right_hip_roll": 8, "r_knee": 9, "right_knee": 9,
        "r_ankle_pitch": 10, "right_ankle_pitch": 10, "r_ankle_roll": 11, "right_ankle_roll": 11,
        "waist_yaw": 12, "torso_yaw": 12, "waist_pitch": 13, "torso_pitch": 13,
        "l_shoulder_yaw": 14, "left_shoulder_yaw": 14,
        "l_shoulder_pitch": 15, "left_shoulder_pitch": 15,
        "l_shoulder_roll": 16, "left_shoulder_roll": 16,
        "l_elbow": 17, "left_elbow": 17, "l_wrist_pitch": 18, "left_wrist_pitch": 18,
        "l_wrist_roll": 19, "left_wrist_roll": 19, "r_shoulder_yaw": 20,
        "right_shoulder_yaw": 20, "r_shoulder_pitch": 21, "right_shoulder_pitch": 21,
        "r_shoulder_roll": 22, "right_shoulder_roll": 22, "r_elbow": 23,
        "right_elbow": 23, "r_wrist_pitch": 24, "right_wrist_pitch": 24,
        "r_wrist_roll": 25, "right_wrist_roll": 25, "neck_yaw": 26,
        "head_yaw": 26, "neck_pitch": 27, "head_pitch": 27,
        "neck_roll": 28, "head_roll": 28,
    }

    def __init__(self, domain_id: int = 0, topics: Optional[Dict[str, str]] = None):
        self.domain_id = domain_id
        self.topics = topics or {}
        self.callbacks: Dict[str, List[Callable]] = {}
        self._running = False
        self._rclpy = None
        self._node = None
        self._executor = None
        self._spin_thread: Optional[threading.Thread] = None
        self._owns_rclpy_context = False
        self._callback_loop: Optional[asyncio.AbstractEventLoop] = None
        self._latest_joint_groups: Dict[str, List[JointState]] = {}
        self._latest_battery_voltage = 0.0
        self._latest_battery_current = 0.0

    def register_callback(self, topic: str, callback: Callable) -> None:
        if topic not in self.callbacks:
            self.callbacks[topic] = []
        self.callbacks[topic].append(callback)
        logger.info(f"已注册回调: {topic}")

    async def start(self) -> None:
        """启动 ROS2 订阅"""
        try:
            import rclpy
            from rclpy.executors import MultiThreadedExecutor
            from rclpy.node import Node
            from rclpy.qos import (
                DurabilityPolicy,
                HistoryPolicy,
                QoSProfile,
                ReliabilityPolicy,
            )
            from aimdk_msgs.msg import JointStateArray, PmuState

            self._rclpy = rclpy
            self._owns_rclpy_context = not rclpy.ok()
            if self._owns_rclpy_context:
                rclpy.init(args=None)

            self._node = Node("manastone_diag_x2_bridge")
            qos = QoSProfile(
                history=HistoryPolicy.KEEP_LAST,
                depth=10,
                # X2 topics are published with BEST_EFFORT (rosbag metadata reliability=2)
                reliability=ReliabilityPolicy.BEST_EFFORT,
                durability=DurabilityPolicy.VOLATILE,
            )

            for topic_key in self._TOPIC_TO_OFFSET:
                topic_name = self.topics.get(topic_key)
                if not topic_name:
                    continue
                self._node.create_subscription(
                    JointStateArray,
                    topic_name,
                    lambda msg, k=topic_key: self._on_joint_msg(k, msg),
                    qos,
                )
                logger.info(f"已订阅关节话题: {topic_name} ({topic_key})")

            pmu_topic = self.topics.get("pmu_state")
            if pmu_topic:
                self._node.create_subscription(PmuState, pmu_topic, self._on_pmu_msg, qos)
                logger.info(f"已订阅电源话题: {pmu_topic}")

            self._executor = MultiThreadedExecutor()
            self._executor.add_node(self._node)
            self._callback_loop = asyncio.get_running_loop()
            self._running = True
            self._spin_thread = threading.Thread(target=self._spin, daemon=True)
            self._spin_thread.start()
            logger.info("ROS2 订阅器已启动")

        except ImportError:
            logger.error("缺少 ROS2 依赖，请确保 rclpy 和 aimdk_msgs 已安装")
            raise
        except Exception as e:
            logger.error(f"ROS2 启动错误: {e}")
            raise

    def _spin(self) -> None:
        if self._executor is None:
            return
        while self._running and self._rclpy and self._rclpy.ok():
            self._executor.spin_once(timeout_sec=0.2)

    def _on_joint_msg(self, topic_key: str, msg: Any) -> None:
        self._latest_joint_groups[topic_key] = self._convert_joint_group(topic_key, msg)
        motor_state: List[JointState] = []
        for key in ("joint_leg_state", "joint_waist_state", "joint_arm_state", "joint_head_state"):
            motor_state.extend(self._latest_joint_groups.get(key, []))
        motor_state.sort(key=lambda j: j.joint_id)

        lowstate = LowState(
            level_flag=1,
            comm_version=1,
            robot_id=2,
            motor_state=motor_state,
            bms_state={
                "battery_voltage": self._latest_battery_voltage,
                "battery_current": self._latest_battery_current,
            },
            timestamp=time.time(),
        )
        self._emit_lowstate(lowstate)

    def _on_pmu_msg(self, msg: Any) -> None:
        self._latest_battery_voltage = float(getattr(msg, "battery_voltage", 0.0))
        self._latest_battery_current = float(getattr(msg, "battery_current", 0.0))

    def _convert_joint_group(self, topic_key: str, msg: Any) -> List[JointState]:
        base = self._TOPIC_TO_OFFSET[topic_key]
        out: List[JointState] = []
        for idx, joint in enumerate(getattr(msg, "joints", [])):
            name = str(getattr(joint, "name", "")).lower().replace("-", "_")
            joint_id = self._NAME_TO_JOINT_ID.get(name, base + idx)
            temp = float(
                max(
                    getattr(joint, "coil_temp", 0.0),
                    getattr(joint, "motor_temp", 0.0),
                )
            )
            out.append(
                JointState(
                    joint_id=joint_id,
                    position=float(getattr(joint, "position", 0.0)),
                    velocity=float(getattr(joint, "velocity", 0.0)),
                    torque=float(getattr(joint, "effort", 0.0)),
                    temperature=temp,
                    timestamp=time.time(),
                )
            )
        return out

    def _emit_lowstate(self, lowstate: LowState) -> None:
        topic = "rt/lf/lowstate"
        if self._callback_loop and self._callback_loop.is_running():
            asyncio.run_coroutine_threadsafe(
                _dispatch_callbacks(self.callbacks, topic, lowstate),
                self._callback_loop,
            )

    async def stop(self) -> None:
        """停止 ROS2 订阅"""
        self._running = False
        if self._spin_thread:
            self._spin_thread.join(timeout=2.0)
            self._spin_thread = None
        if self._executor and self._node:
            self._executor.remove_node(self._node)
            self._executor.shutdown()
        if self._node:
            self._node.destroy_node()
            self._node = None
        if self._owns_rclpy_context and self._rclpy and self._rclpy.ok():
            self._rclpy.shutdown()
        logger.info("ROS2 订阅器已停止")


async def _dispatch_callbacks(
    callbacks: Dict[str, List[Callable]],
    topic: str,
    message: LowState,
) -> None:
    if topic not in callbacks:
        return
    for cb in callbacks[topic]:
        try:
            if asyncio.iscoroutinefunction(cb):
                await cb(message)
            else:
                cb(message)
        except Exception as e:
            logger.error(f"回调执行错误: {e}")


class DDSBridge:
    """DDS Bridge - 统一接口"""
    
    def __init__(self):
        self.config = get_config()
        self.cache = DDSCache(
            max_size=self.config.cache.max_size,
            window_seconds=self.config.cache.window_seconds
        )
        self._subscriber: Optional[DDSSubscriber | MockDDSSubscriber] = None
    
    async def start(self) -> None:
        """启动 DDS Bridge"""
        if self.config.mock_mode:
            self._subscriber = MockDDSSubscriber()
            await self._subscriber.start()
            return

        try:
            self._subscriber = DDSSubscriber(
                domain_id=self.config.dds.domain_id,
                topics=self.config.dds.topics,
            )

            # 注册缓存回调
            self._subscriber.register_callback(
                "rt/lf/lowstate",
                self._on_lowstate
            )
            await self._subscriber.start()
        except ModuleNotFoundError as e:
            # UI / 开发机上缺少 ROS2 运行时时，自动降级为模拟模式，避免服务启动失败
            if e.name in {"rclpy", "aimdk_msgs"}:
                logger.warning(
                    "缺少 ROS2 依赖 (%s)，自动切换到 mock 模式。"
                    "如需连接真机，请先 source ROS2 环境并安装对应消息包。",
                    e.name,
                )
                self._subscriber = MockDDSSubscriber()
                self._subscriber.register_callback(
                    "rt/lf/lowstate",
                    self._on_lowstate
                )
                await self._subscriber.start()
                return
            raise
    
    def set_scenario(self, scenario_name: str) -> bool:
        """切换 mock 场景（仅 mock 模式有效）"""
        if not isinstance(self._subscriber, MockDDSSubscriber):
            return False
        from .mock_scenarios import ScenarioType
        try:
            self._subscriber.scenario = ScenarioType(scenario_name)
            return True
        except ValueError:
            return False

    def get_scenario(self) -> str | None:
        if isinstance(self._subscriber, MockDDSSubscriber):
            return self._subscriber.scenario.value
        return None

    async def stop(self) -> None:
        """停止 DDS Bridge"""
        if self._subscriber:
            await self._subscriber.stop()
    
    async def _on_lowstate(self, data: LowState) -> None:
        """处理 LowState 消息"""
        await self.cache.put(data)
    
    async def get_latest_joints(self) -> Optional[List[JointState]]:
        """获取最新关节状态"""
        latest = await self.cache.get_latest()
        if latest and hasattr(latest, 'motor_state'):
            return latest.motor_state
        return None
    
    async def get_joint_trend(self, joint_id: int, field: str = "temperature", seconds: int = 600) -> Dict:
        """获取指定关节的趋势"""
        recent = await self.cache.get_recent(seconds)
        values = []
        
        for data in recent:
            if hasattr(data, 'motor_state'):
                for joint in data.motor_state:
                    if joint.joint_id == joint_id:
                        if hasattr(joint, field):
                            values.append(getattr(joint, field))
                        break
        
        if not values:
            return {"start": 0, "end": 0, "min": 0, "max": 0, "avg": 0, "count": 0}
        
        return {
            "start": values[0],
            "end": values[-1],
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / len(values),
            "count": len(values)
        }
