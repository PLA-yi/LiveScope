#!/usr/bin/env bash
# 编译抖音 Protobuf 定义文件
# 依赖：pip install grpcio-tools
set -e

cd "$(dirname "$0")/.."

echo "Compiling douyin.proto …"
python -m grpc_tools.protoc \
    -I proto \
    --python_out=src/proto \
    proto/douyin.proto

mkdir -p src/proto
touch src/proto/__init__.py

echo "Done → src/proto/douyin_pb2.py"
