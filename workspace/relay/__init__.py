"""船岸断网中继器（ship-shore disconnect relay）。

- protocol : 段包线格式、校验和、记录合法性
- segment  : 手持终端只追加段文件与封包/重发
- store    : 接收端 SQLite 状态库（原始包/游标/记录/待处理/处置/事件日志）
- receiver : 接收服务（顺序、去重、隔离、断层、现有装船确认入口对接、恢复）
- console  : 操作台查询与处置
- emulator : 边缘模拟器
"""
