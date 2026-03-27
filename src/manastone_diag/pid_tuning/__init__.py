"""
pid_tuning — AI 驱动的 PID 自动调参子系统

模块结构：
  scorer.py     阶跃响应质量评分（量化"好不好"）
  safety.py     安全围栏（防止参数超边界导致硬件损坏）
  experiment.py 实验运行器（仿真 + 真机两种模式）
  optimizer.py  搜索策略辅助（历史管理 + LLM 提示构建）
"""
