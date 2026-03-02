#!/bin/bash
# 切换到脚本所在目录
cd "$(dirname "$0")"

# 运行启动器
python3 start.py

# 结束后暂停，避免终端窗口立即关闭
echo ""
echo "  按任意键关闭窗口 …"
read -n 1
