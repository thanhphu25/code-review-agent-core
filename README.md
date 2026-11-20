# Code Review Agent Core

Lõi Python cho agent review code dùng LangGraph. Repo cung cấp vòng gọi model và tool, schema kết quả, kiểm tra bằng chứng và CLI demo. Bản đầu chạy một ca liên file bằng fixture để kiểm tra cách agent chọn tool, dùng kết quả và dừng trong giới hạn lượt.

Các adapter graph và model sẽ dùng cùng `run_review()`. Benchmark các nguồn ngữ cảnh sẽ chạy trên lõi này khi có adapter thật.

## Chạy local

Trong thư mục chứa `pyproject.toml` của app, tạo môi trường Python riêng và cài package.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/review-agent demo
.venv/bin/python -m unittest discover -s tests -v
```

Demo dự kiến trả JSON với `status: complete`, ba lượt model, hai lượt tool và một finding tại `reports/monthly.py:5`. Model trong demo được viết theo kịch bản. Kết quả này kiểm tra cơ chế của graph trước khi đo chất lượng review với model thật.

## Cấu trúc

- `graph.py` định nghĩa vòng `reviewer → call_tools → reviewer`. Hàm `run_review()` nhận `ReviewInput` gồm diff và SHA, cùng ba hàm gọi model, đọc file và lấy ngữ cảnh graph. Các schema Pydantic kiểm tra đầu vào tool và kết quả. Finding có quote lệch source sẽ bị loại và run ghi gap.
- `demo.py` cung cấp model, source và graph giả. Ở lượt thứ hai, model giả chọn file cần đọc từ đường dẫn tool vừa trả.
- `cli.py` kết nối core với lệnh `review-agent demo`. Output gồm findings, gaps, SHA và trace để dùng cho đánh giá sau này.
- `tests/test_core.py` kiểm tra điều tra liên file, giới hạn lượt, ghép kết quả theo ID của tool call và xử lý output lỗi.

## Chạy trên server

Khi clone app từ GitHub lên server, chạy trong thư mục có `Dockerfile`.

```bash
docker build -t code-review-agent-core .
docker run --rm --network none code-review-agent-core demo
```

Container chạy lệnh và thoát sau khi in JSON. Ca demo chạy độc lập, nên server có thể kiểm tra image trước khi cấu hình endpoint hoặc credentials cho các provider thật.
