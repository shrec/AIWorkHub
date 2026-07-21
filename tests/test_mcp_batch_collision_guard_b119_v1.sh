#!/bin/bash
set -e

echo "Testing MCP Batch Collision Guard V1..."

echo "Generating mock data..."
cat << 'JSONL' > tools/geoai-task-mcp/eval/mcp_batch_collision_guard_rows_b119_v1.jsonl
{"task_id": "T1", "status": "pending", "runner": "R1", "topic": "t1", "allowed_writes": ["file_a", "file_b"]}
{"task_id": "T2", "status": "processing", "runner": "R2", "topic": "t2", "allowed_writes": ["file_b", "file_c"]}
{"task_id": "T3", "status": "pending", "runner": "R1", "topic": "t1", "allowed_writes": ["file_d"]}
{"task_id": "T_FINISHED", "status": "finished", "runner": "R1", "topic": "t1", "allowed_writes": ["file_a"]}
JSONL

# 1. Run Pre-Wave (should fail due to T1/T2 colliding on file_b)
echo "1. Testing Pre-Wave mode on conflicting rows..."
if python3 tools/geoai-task-mcp/scripts/build_mcp_batch_collision_guard_b119_v1.py --cards tools/geoai-task-mcp/eval/mcp_batch_collision_guard_rows_b119_v1.jsonl --mode pre_wave --out tools/geoai-task-mcp/eval/mcp_batch_collision_guard_b119_v1.json; then
    echo "FAIL: Pre-wave should have failed due to collision."
    exit 1
fi
echo "Pre-wave failed as expected (collision caught)."

# Inspect suggested action
if ! grep -q "BLOCKED Action: De-duplicate allowed_writes" tools/geoai-task-mcp/eval/mcp_batch_collision_guard_b119_v1.json; then
    echo "FAIL: Missing blocked action recommendation."
    exit 1
fi

# 2. Run a clean scenario
cat << 'JSONL' > tools/geoai-task-mcp/eval/mcp_batch_collision_guard_clean_rows_b119_v1.jsonl
{"task_id": "T4", "status": "pending", "runner": "R1", "topic": "t1", "allowed_writes": ["file_a"]}
{"task_id": "T5", "status": "pending", "runner": "R2", "topic": "t2", "allowed_writes": ["file_b"]}
JSONL

echo "2. Testing pre-wave clean..."
if ! python3 tools/geoai-task-mcp/scripts/build_mcp_batch_collision_guard_b119_v1.py --cards tools/geoai-task-mcp/eval/mcp_batch_collision_guard_clean_rows_b119_v1.jsonl --mode pre_wave --out tools/geoai-task-mcp/eval/mcp_batch_collision_guard_b119_v1.json; then
    echo "FAIL: Pre-wave should have passed cleanly."
    exit 1
fi
echo "Clean passed."

echo "3. Testing post-wave mode on clean..."
if ! python3 tools/geoai-task-mcp/scripts/build_mcp_batch_collision_guard_b119_v1.py --cards tools/geoai-task-mcp/eval/mcp_batch_collision_guard_clean_rows_b119_v1.jsonl --mode post_wave --out tools/geoai-task-mcp/eval/mcp_batch_collision_guard_b119_v1.json; then
    echo "FAIL: Post-wave should have passed cleanly."
    exit 1
fi
rm tools/geoai-task-mcp/eval/mcp_batch_collision_guard_clean_rows_b119_v1.jsonl

echo "✅ All tests passed"
