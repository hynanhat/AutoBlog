# OCR Hiragana

Chép ảnh vào `inbox` để tự động lọc nhiễu bằng OpenCV, đọc Hiragana bằng Gemini và lấy chữ đã chuẩn hóa qua HTTP. Dịch vụ xử lý tối đa 10 ảnh đồng thời. Giao diện Streamlit cho phép xem từng bước lọc và so sánh kết quả với nhãn.

## Chạy trên Windows

Trong PowerShell, từ thư mục dự án, tạo môi trường Python 3.10 trở lên nếu cần và cài thư viện.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Nếu dùng Python 3.10, thay `-3.12` bằng `-3.10`. Đặt `GEMINI_API_KEY` trong môi trường hoặc trong tệp `.env` ở thư mục dự án. Khi tạo cấu hình mới, có thể sao chép từ `.env.example`. Đặt `GEMINI_MODEL` nếu cần chọn model khác mặc định `gemini-2.5-flash`.

### Theo dõi thư mục và trả kết quả qua HTTP

Để giao việc tích hợp cho AI hoặc lập trình viên, xem [hướng dẫn lấy kết quả OCR cho ứng dụng khác](HUONG_DAN_TICH_HOP_OCR.md).

```powershell
.\.venv\Scripts\python.exe server.py
```

Dịch vụ tạo thư mục `inbox` và mở cổng `127.0.0.1:8000`. Chép một hoặc nhiều ảnh PNG, JPG, JPEG trực tiếp vào thư mục này. Mỗi tệp ổn định qua hai lần quét được đưa vào hàng đợi, kể cả ảnh đã có sẵn khi khởi động. Với chương trình ghi ảnh theo từng phần, hãy ghi vào tệp đuôi `.part` rồi đổi sang tên ảnh cuối khi ghi xong. Tệp gốc được giữ để đối chiếu.

Dịch vụ gửi ảnh đã lọc tới Google, xử lý tối đa 10 ảnh đồng thời và nhận tiếp ảnh đang chờ khi có luồng rảnh. Để đổi thư mục, cổng hoặc khoảng cách giữa các lần gửi yêu cầu Gemini, dùng các tham số như sau.

```powershell
.\.venv\Scripts\python.exe server.py --inbox "D:\Anh Hiragana" --port 8010 --workers 10 --delay 1
```

| Yêu cầu HTTP | Dữ liệu trả về |
| --- | --- |
| `GET /health` | Trạng thái theo dõi, đường dẫn thư mục, số luồng và model |
| `GET /jobs` | Các ảnh đã tiếp nhận, sắp theo `id`, gồm cả `queued` và `processing` |
| `GET /jobs/42` | Trạng thái, tên tệp và kết quả của ảnh có `id` bằng 42. Trả HTTP 404 nếu chưa có |
| `GET /results?after=0&limit=100` | Kết quả đã có, sắp theo `cursor`, gồm cả `done` và `failed` |

Ví dụ dưới đây in tên ảnh và chữ đã chuẩn hóa bằng PowerShell. Chạy sau khi chép ảnh vào `inbox`.

```powershell
$page = Invoke-RestMethod 'http://127.0.0.1:8000/results?after=0'
$page.items | Select-Object filename, status, @{Name='hiragana'; Expression={$_.result.normalized_text}}, error
```

Trong ứng dụng nhận, bắt đầu với `after=0`. Sau khi xử lý thành công mỗi trang, lưu `next_cursor` rồi dùng giá trị đó làm `after` cho lần gọi tiếp theo. Trang rỗng có `items: []` và giữ nguyên con trỏ. Cách này lấy được cả ảnh hoàn tất muộn. Mỗi trang nhận từ 1 đến 100 mục theo `limit`.

Với `status="done"`, dùng `result.normalized_text` để lấy chữ đã bỏ dấu đục, dấu bán đục và đổi chữ nhỏ thành chữ thường. Ví dụ `result.text` là `はひじぬせさ` thì `result.normalized_text` là `はひしぬせさ`. Trường `result.characters` chứa romaji và `result.warnings` chứa cảnh báo từ kết quả OCR. Với `status="failed"`, đọc `error` để biết lý do. Có thể gửi lại ảnh dưới tên mới sau khi sửa tệp hoặc khi hạn mức Gemini được cấp lại.

Lịch sử và ảnh đang chờ nằm trong `inbox/.ocr-state/jobs.sqlite3`. Khởi động lại với cùng thư mục để đọc tiếp lịch sử và xử lý các tác vụ còn dang dở. Dùng `Ctrl+C` để dừng khi chạy dịch vụ trong cửa sổ PowerShell.

### Mở giao diện Streamlit

```powershell
.\.venv\Scripts\python.exe -m streamlit run ocr_ui.py
# Hoặc từ thư mục gốc:
.\.venv\Scripts\python.exe -m streamlit run ocr/ocr_ui.py
```

Mở địa chỉ mà Streamlit hiển thị. Giao diện nhận khóa từ `.env` hoặc thanh bên. Phần xem trước và tải ảnh trung gian hoạt động ngoại tuyến.

## Sử dụng

**Định dạng lại Hiragana** chạy ngay trên máy. Trong tab **Một ảnh**, mở **Định dạng lại văn bản đã có** và dán chuỗi từ lần nhận diện trước. Ví dụ, `はひじぬせさ` cho `はひしぬせさ`, còn `ちつぇゅぅさ` cho `ちつえゆうさ`. Mỗi chữ nhỏ được đổi thành một chữ thường tại cùng vị trí.

Tab **Một ảnh** mặc định hiển thị chữ đã chuẩn hóa trước. Mở **Xem kết quả OCR ban đầu và romaji** để đối chiếu. Trong tab **Hàng loạt** và **Đánh giá**, chọn **Hiện chữ đã chuẩn hóa** để thêm cột tương ứng. Bật, tắt và tải tệp dùng kết quả đã lưu trong phiên, không tốn thêm lượt Gemini. Bảng romaji và điểm CER/Exact Match luôn dựa trên dự đoán OCR ban đầu.

Bạn có thể lưu tên model vào `GEMINI_MODEL` trong `.env` để dùng chung cho giao diện và CLI. Giá trị nhập ở thanh bên hoặc tham số `--model` được ưu tiên cho lượt chạy đó. Khi bỏ trống, ứng dụng dùng `gemini-2.5-flash` theo kế hoạch ban đầu.

- **Một ảnh.** Chọn PNG/JPEG hoặc ảnh mẫu. Chế độ Auto tính ngưỡng dựa trên histogram. Chế độ Manual cho phép chỉnh ngưỡng từ 20 đến 120. Tăng kích thước nhân hoặc diện tích tối thiểu để lọc mạnh hơn, rồi kiểm tra các dấu và nét nhỏ trong ảnh xem trước. Chọn **Đọc chữ từ ảnh sạch** hoặc **Đọc chữ từ ảnh gốc**. Mỗi lựa chọn gửi một ảnh với thời gian chờ cấu hình 45 giây. Bạn có thể đọc ảnh còn lại để so sánh. Kết quả đã có được dùng lại khi bấm lần nữa. Kết quả gồm chuỗi liền, chuỗi tách khoảng trắng, bảng romaji, ghi chú và cảnh báo độ dài. Có thể tải TXT, JSON hoặc PNG của từng bước.
- **Hàng loạt.** Chọn nhiều ảnh hoặc cả thư mục. Ứng dụng gửi các ảnh sạch chưa có trong bộ nhớ OCR của phiên. Bảng kết quả ghi riêng ảnh lỗi, cho phép tải CSV và JSON.
- **Đánh giá.** Dùng bộ mẫu đi kèm hoặc tải CSV và ảnh tương ứng. Hai cột bắt buộc là `filename` và `ground_truth`. Mỗi tên là duy nhất, nhãn phải có sáu ký tự. UI ghép theo tên tệp tải lên. CLI cũng hỗ trợ tên tương đối có thư mục con. Ảnh thiếu hoặc lỗi sẽ dừng phép so sánh để hai chế độ có cùng mẫu số. Ảnh ngoài danh sách nhãn được bỏ qua.

## Chạy thực nghiệm từ dòng lệnh

```powershell
.\.venv\Scripts\python.exe evaluate.py --images data/samples --labels data/labels.csv --out reports --delay 1
```

Đặt khóa trong môi trường hoặc `.env` trước khi chạy. Có thể chọn model với `--model`. Kết quả gồm dự đoán từng ảnh trong `predictions.csv`, điểm tổng hợp trong `metrics.csv`, các phép thay thế đã sắp theo tần suất trong `confusions.csv` và biểu đồ `comparison.png` ở 300 dpi. Tệp `performance.json` ghi thời gian OCR, số lần gửi yêu cầu kể cả thử lại, số lần dùng bộ nhớ đệm và tổng token Gemini báo về. Trường `responses_with_usage` cho biết có bao nhiêu phản hồi cung cấp số token.

Hàng loạt và đánh giá mặc định xử lý hai ảnh đồng thời. Có thể chọn từ 1 đến 10 bằng `--workers` hoặc **Số ảnh xử lý đồng thời** trên thanh bên. Khoảng cách `--delay` áp dụng chung cho các lần bắt đầu yêu cầu trong một lượt chạy.

Ba tab dùng chung tối đa 256 kết quả OCR trong phiên. Hai tệp có cùng dữ liệu pixel, kích thước, kiểu dữ liệu và model có thể dùng lại kết quả dù tên tệp khác nhau. Bấm **Xóa kết quả OCR trong phiên** để đo lại từ đầu. Mỗi lần chạy CLI có bộ nhớ riêng.

**CER** bằng tổng khoảng cách Levenshtein chia tổng số ký tự nhãn, tính cả thay thế, thêm và xóa. CER thấp hơn là tốt hơn và có thể vượt 100% nếu dự đoán thừa nhiều ký tự. **Exact Match** là tỷ lệ ảnh khớp toàn bộ chuỗi. Các chỉ số trong CSV dùng thang tỷ lệ, biểu đồ dùng phần trăm. Khi có nhiều cách căn chỉnh tối ưu, bảng nhầm lẫn ưu tiên bước chéo, sau đó xóa rồi thêm.

Bộ minh họa gồm ba ảnh chữ in tổng hợp với ba kiểu nền xám, đường mảnh và chấm nhiễu. Nhãn ở `data/labels.csv`. Để báo cáo chất lượng thực tế, chạy cùng lệnh trên tập ảnh và nhãn đại diện của bạn.

## Hiệu năng OCR và token Gemini

Trên ba ảnh mẫu với `gemini-3.1-flash-lite`, hai vòng đọc ảnh gốc và ảnh sạch dùng tổng cộng 6.924 token sau tối ưu, so với 14.510 token trước đó. Báo cáo có thời gian từng lượt, số liệu cho trường hợp đọc lại từ bộ nhớ và lỗi ký tự còn gặp. [Báo cáo tối ưu ngày 20/11/2026](reports/2026-11-20-ocr-gemini/README.md)

## Kết quả đối chứng trên bộ mẫu

Lượt chạy bằng `gemini-3.1-flash-lite` dùng ba ảnh tổng hợp, mỗi ảnh có sáu ký tự. Ảnh gốc có 2 lỗi chỉnh sửa trên 18 ký tự và khớp hoàn toàn 2/3 ảnh. Sau khử nhiễu tự động với thông số mặc định, có 4 lỗi và khớp hoàn toàn 1/3 ảnh. Kết quả này chỉ mô tả ba ảnh mẫu. Xem [dự đoán từng ảnh](reports/gemini-3.1-flash-lite-samples/predictions.csv), [điểm tổng hợp](reports/gemini-3.1-flash-lite-samples/metrics.csv) và [biểu đồ đối chứng](reports/gemini-3.1-flash-lite-samples/comparison.png).

## Kiến trúc

| Tệp | Vai trò |
| --- | --- |
| config.py | Model, ngưỡng và tham số ảnh mặc định |
| preprocess.py | Đọc/ghi ảnh Unicode và trả ảnh đen trên nền trắng cùng sáu bước trung gian |
| ocr_service.py | Tái sử dụng kết quả theo nội dung ảnh, gửi PNG và nhận hai trường chữ và ghi chú qua schema Pydantic, giãn cách yêu cầu và thử lại |
| formatter.py | Chuẩn hóa Unicode, lọc Hiragana, kiểm tra độ dài và tạo romaji cục bộ theo từng ký tự OCR |
| evaluate.py | Ghép ảnh với nhãn, so sánh hai chế độ, tính điểm và xuất báo cáo |
| app.py | Ba tab Streamlit dùng chung pipeline với CLI |
| folder_ocr.py | Phát hiện tệp ổn định, lưu hàng đợi SQLite và điều phối xử lý ảnh |
| server.py | Khởi động dịch vụ theo dõi thư mục và cung cấp API HTTP |

## Kiểm thử và Spec Kit

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Bộ test gọi SDK thật tới máy chủ HTTP mô phỏng trên localhost, kiểm tra retry, schema yêu cầu, các tab Streamlit và tệp CLI. Các dự đoán trong test được định trước để kiểm tra công thức và giao diện.

Kế hoạch gốc nằm ở [Plan.md](Plan.md). Đặc tả, kế hoạch kỹ thuật và danh sách công việc trong [Phase 1](specs/001-denoise-hiragana/) và [Phase 2–4](specs/002-ocr-evaluation/) ghi lại phạm vi đã triển khai.
