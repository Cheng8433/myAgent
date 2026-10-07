"""进程入口 —— 只负责"把零件接上电"，不含任何业务逻辑。

★ 为什么单独一层，而不是让 workflows/daily_report.py 自己带 main：
  编排层要能被【测试】直接调用（m4b_verify 调 run_daily_report，不经过 CLI），
  也要能被别的入口调用（将来的定时任务、web 触发、一条命令跑多份报告）。
  把 sys.argv / load_dotenv / sys.exit 这些"进程级"的东西留在 app/，
  workflows/ 就还是一个可以被 import 的普通库。

  判据：一个模块如果 `sys.exit()` 了，它就不能被 import —— 它会杀掉调用者。
"""
