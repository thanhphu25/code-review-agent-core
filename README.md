# Code Review Agent Core

Lõi Python cho agent review code dùng LangGraph. Repo cung cấp vòng gọi model và tool, schema kết quả, kiểm tra bằng chứng và CLI demo. Bản đầu chạy một ca liên file bằng fixture để kiểm tra cách agent chọn tool, dùng kết quả và dừng trong giới hạn lượt.

Dashboard dùng cùng `run_review()` với các adapter model, CodeLens REST và Graphify MCP.

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

## Dashboard

```bash
.venv/bin/python -m pip install '.[web]'
.venv/bin/review-agent serve --port 8765
```

Mở `http://127.0.0.1:8765`. Chọn **Pricing demo** và bấm **Chạy review** để kiểm tra giao diện bằng fixture. SQLite lưu snapshot và lịch sử tại `review-data/reviews.sqlite3`.

- **GitHub public** nhận `owner/repo`, head ref và base ref tùy chọn. **Kiểm tra thay đổi** lấy commit mới trên branch và lưu snapshot để review. Có thể thử chính `thanhphu25/code-review-agent-core` với base `df68d5b` và head `d76a276`.
- **ZIP** nhận snapshot base và head. Dùng `git archive --format=zip -o head.zip HEAD` để giữ SHA trong ZIP. ZIP thông thường dùng dấu vân tay nội dung. Agent lưu file text để review và giữ gói source đã lọc để gửi Graphify. Mục **File nguồn** liệt kê file text của snapshot mới nhất.
- **Kết quả** hiển thị finding kèm quote, trace tool, token nếu gateway cung cấp và thời gian chạy. Chọn mục lịch sử để mở lại run.

Workspace mở theo từng repository. Dùng **Chat mới** để đặt tên phiên, chọn mode **Graphify** hoặc **CodeLens** và ghim base/head. Gửi yêu cầu trực tiếp trong **Review nhanh / so sánh** cũng tạo phiên cho cặp đang chọn. Các phiên có lịch sử riêng và mở lại từ sidebar hoặc URL. Muốn đổi provider hay snapshot, tạo phiên khác. **So sánh hai provider** nằm trong **Review nhanh / so sánh** và chạy hai lượt độc lập với cùng yêu cầu.

- Với repository GitHub public, chọn **Nhập commit hoặc ref GitHub** trong **Chat mới** để so sánh hai commit bất kỳ. Agent phân giải branch, tag hoặc SHA thành hai SHA cố định, tải snapshot và giữ nguyên ref theo dõi của repository. Với ZIP, nhập hai bản bằng **Thêm ZIP** trước khi chọn cặp.
- Trong hộp **Chat mới**, gắn tối đa bốn repo đã nhập. Chọn head để agent đọc ngữ cảnh. Chọn thêm base nếu muốn đưa diff của repo đó vào lượt review.
- Repo liên quan dùng snapshot và index của chính nó. Với Graphify Pilot tự động, agent đưa các head đã ghim vào hàng đợi. Với CodeLens, cấu hình liên kết project/branch cho từng repo cần truy vấn. Agent có thể đọc và tìm trong snapshot liên quan khi graph của repo đó chưa sẵn sàng. Trace ghi lý do nếu một lần truy vấn graph thiếu index.
- Finding và trace liên repo ghi repo cùng SHA đã dùng để kiểm chứng. **Xem diff** nằm ở snapshot repo chính và dưới từng repo liên quan có base. Lịch sử các lượt cũ và so sánh nằm trong **Review nhanh / so sánh**.

Khi bind ngoài loopback, đặt `REVIEW_DASHBOARD_PASSWORD` rồi đăng nhập bằng username `review`. Chọn **Kết nối** để cấu hình URL và key của model, Graphify hoặc CodeLens. Cấu hình áp dụng từ lượt review tiếp theo và được lưu trong cơ sở dữ liệu của dashboard, với quyền đọc/ghi dành cho tài khoản chạy agent. Key chỉ hiển thị trạng thái đã cấu hình. Để trống ô key để giữ giá trị hoặc dùng hộp chọn để xóa. Cấu hình đã lưu có ưu tiên hơn biến môi trường.

Nếu Graphify Pilot và agent chạy trực tiếp trên cùng máy, có thể dùng chung port trên hai địa chỉ khác nhau. Bind Graphify vào `127.0.0.1:18081` với origin `http://127.0.0.1:18081`, và bind agent vào đúng IP LAN của máy, ví dụ `review-agent serve --host 192.168.1.10 --port 18081`. Trong **Kết nối**, điền MCP URL dạng `http://127.0.0.1:18081/mcp/PROJECT_ID`. Dùng HTTPS hoặc SSH tunnel khi nhập key qua trình duyệt.

```bash
docker build -t code-review-agent-core .
docker run --rm -p 127.0.0.1:8765:8765 --env-file /path/to/review.env \
  -v review-agent-data:/data code-review-agent-core \
  serve --host 0.0.0.0 --port 8765 --database /data/reviews.sqlite3
```

### CodeLens trước

| Biến | Giá trị |
|---|---|
| `REVIEW_AGENT_LLM_BASE_URL` | URL LiteLLM/OpenAI-compatible |
| `REVIEW_AGENT_LLM_API_KEY` | Key của gateway |
| `REVIEW_AGENT_LLM_MODEL` | Model alias hỗ trợ tool calling |
| `CODELENS_API_BASE` | REST base như `https://codelens.example/api/v1/rag` |
| `CODELENS_API_TOKEN` | Token REST của CodeLens |

Các biến trong bảng dùng làm mặc định. Ba biến `CODEDECK_AGENT_LLM_*` của bản thử trước cũng dùng được nếu đã cấu hình.

Sau khi CodeLens index branch, mở **Cấu hình CodeLens**, nhập GitLab project ID và branch. Nếu REST context không trả `indexed_sha`, nhập thêm CodeLens branch ID để dashboard so `head_sha` của index trước và sau truy vấn. SHA lệch trả gap và loại dữ liệu đó. Khi thiếu SHA, dữ liệu chỉ làm gợi ý và kết quả ghi `incomplete`.

### Graphify và so sánh

Trong **Kết nối → Graphify Pilot · tự động index**, nhập URL gốc của Pilot (ví dụ `http://127.0.0.1:18081`), tài khoản Pilot và mật khẩu. Ba giá trị này cũng có thể cấu hình qua `GRAPHIFY_PILOT_URL`, `GRAPHIFY_PILOT_USER` và `GRAPHIFY_PILOT_PASSWORD`. Dùng tài khoản Pilot riêng cho agent vì mỗi project trên Pilot chỉ có một token MCP đang hoạt động.

- Chọn **Graphify** khi thêm repo. Với ZIP, có thể nhập base và head cùng lúc. Agent chuyển toàn bộ cây source của head đã nhập sang Pilot. Với GitHub public, agent lấy ZIP theo SHA của head và thực hiện cùng luồng. Thêm một snapshot với vai trò **Base** để giữ nó trong SQLite mà không gửi index lúc upload.
- Agent theo dõi job của Pilot và tự cấp token MCP khi project sẵn sàng. Mỗi snapshot giữ liên kết Graphify riêng trong SQLite, nên có thể mở lại repo sau khi restart. Trace ghi `graphify_agent_upload` khi SHA được đối chiếu theo snapshot agent đã gửi.
- Với snapshot cũ, agent dựng lại ZIP từ các file text còn trong SQLite. Nhập lại bản ZIP nguồn nếu cần index phần source mà snapshot cũ đã bỏ qua. Số project vẫn theo giới hạn của tài khoản Pilot.
- Để dùng MCP cấu hình sẵn, dùng phần **Graphify · MCP thủ công** và để trống kết nối tự động của Pilot. Có thể dùng `GRAPHIFY_MCP_URL`, `GRAPHIFY_API_KEY` và `GRAPHIFY_INDEXED_SHA` làm mặc định. Trường **SHA đã index** được dùng khi phản hồi MCP thiếu revision.

Chọn cùng base/head và bấm **So sánh hai provider**. Hai run nhận cùng diff, source, model và ngân sách tool. Dashboard đặt findings, thời gian, token và số lần truy vấn khớp SHA cạnh nhau. Có thể gọi riêng từng provider trong cùng workspace.

### Ca benchmark có nhãn

- [AACR-Bench](https://github.com/alibaba/aacr-bench) phù hợp để chọn PR mà finding cần ngữ cảnh liên file. Phương pháp chấm và nhãn nằm trong [bài nghiên cứu](https://arxiv.org/abs/2601.19494).
- [ReviewBench](https://github.com/review-bench/ReviewBench) công bố bộ test 25 PR, nhãn human-reviewed và mirror chứa commit base/head. Có thể nhập mirror và hai SHA trực tiếp vào dashboard.

Ca Python `AA-Factory/aafactory-prototype#17` đã được kiểm tra tải qua GitHub và dựng diff trong giới hạn dashboard. Dùng các giá trị từ [manifest ReviewBench](https://github.com/review-bench/ReviewBench/blob/main/corpus/test/test.json).

| Trường | Giá trị |
|---|---|
| GitHub owner/repo | `review-bench/AA-Factory_aafactory-prototype` |
| Base ref | `e7f558c7360abac335af2df22f4ed376992b30ab` |
| Head ref | `a1978cb75950a879df8805e4a5f373eb6678f6f9` |

Index đúng head ở cả CodeLens và Graphify rồi chọn hai provider. Giữ [golden findings](https://github.com/review-bench/ReviewBench/blob/main/corpus/test/findings.json) riêng để chấm sau khi review.

Ghim danh sách case, alias model, snapshot của hai graph và giới hạn lượt trước khi chạy. So findings với golden labels bằng root cause và vị trí, rồi ghi precision, recall và F1. Dashboard hiện lưu kết quả và số đo vận hành cho từng ca. Việc chấm theo golden labels dùng rubric của bộ benchmark đã chọn.
