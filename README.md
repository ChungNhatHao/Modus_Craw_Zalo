# Zalo Order Crawler

Tool điều khiển Zalo Web bằng Playwright, mở đúng nhóm, lọc tin nhắn theo ngày,
cuộn qua danh sách tin nhắn dạng virtualized DOM, làm sạch HTML bằng BeautifulSoup
và dùng Gemini để nhận diện các tin nhắn liên quan đến đơn hàng.

## Giao diện web cục bộ

Khởi động giao diện bằng lệnh:

```bash
.venv/bin/zalo-order-crawler ui
```

Tool tự mở `http://127.0.0.1:8765/`. Giao diện chỉ bind vào loopback để API key,
cookie và dữ liệu Zalo không bị đưa ra mạng LAN.

Quy trình sử dụng:

1. Bấm **Mở Zalo để đăng nhập**. Một cửa sổ Chrome dùng hồ sơ Playwright riêng sẽ mở.
2. Tự đăng nhập và đồng bộ tin nhắn cần thiết trong Zalo.
3. Quay lại giao diện, bấm **Đã đăng nhập & đồng bộ xong**. Tool lưu hồ sơ và đóng
   cửa sổ Zalo an toàn.
4. Nhập danh sách nhóm, mỗi dòng một tên. Tên trùng và dòng trống được tự bỏ qua.
5. Chọn ngày cần crawl; mặc định là hôm nay theo `Asia/Ho_Chi_Minh`.
6. Bấm **Bắt đầu crawl dữ liệu**. Các nhóm được xử lý tuần tự và kết quả từng nhóm
   xuất hiện trong phần tiến trình.
7. Khi hoàn tất, phần **Kết quả AI** tự hiển thị số tin, số đơn, ảnh đính kèm,
   nội dung AI đánh giá, sản phẩm, số lượng và độ tin cậy. Có thể lọc giữa đơn hàng,
   tất cả tin và các tin không phải đơn; bấm ảnh để xem kích thước đầy đủ.
8. Tin nhắn chữ được thêm vào Google Sheet `DD-MM-YYYY`; ảnh tin nhắn được tải lên
   folder `DD-MM-YYYY_image`. Link mở Sheet và folder ảnh xuất hiện ở kết quả từng
   nhóm trên UI.

Mỗi đơn có hai chỉ số riêng: **Nhận diện đơn** cho quyết định đây có phải đơn hàng
hay không và **Thông tin đơn** cho độ tin cậy của dữ liệu đã trích xuất. Đơn dưới 90%
hoặc thiếu sản phẩm/số lượng được gắn nhãn **Cần kiểm tra**. Đây là độ tin cậy do AI
và quy tắc kiểm tra tính nhất quán tạo ra, không phải accuracy thực tế đã hiệu chuẩn.

Có thể dùng cổng khác hoặc không tự mở trang UI:

```bash
.venv/bin/zalo-order-crawler ui --port 8899 --no-open
```

## Luồng xử lý

1. Dùng một hồ sơ trình duyệt riêng có lưu phiên đăng nhập, hoặc kết nối Chrome/Edge
   đã bật Chrome DevTools Protocol (CDP).
2. Tìm và mở nhóm theo tên chính xác; kiểm tra lại tiêu đề để tránh crawl nhầm nhóm.
3. Mở **Tìm kiếm tin nhắn**, chọn **Ngày gửi**, rồi chọn ngày yêu cầu.
4. Đi ngược tới ranh giới đầu ngày, sau đó cuộn xuôi và thu thập từng node tin nhắn.
5. Tải ảnh tin nhắn khi phiên Playwright còn mở, lưu vào `assets/` và thay URL
   `blob:` trong HTML bằng đường dẫn cục bộ. Thumbnail link được lưu riêng và không
   bị coi là ảnh đơn hàng.
6. Lưu HTML/CSS cục bộ, dùng BeautifulSoup loại nút, reaction, icon, thời gian và
   các nội dung giao diện không cần thiết.
7. Gửi nội dung đã làm sạch, metadata và ảnh tin nhắn sang Gemini. Gemini trả JSON
   theo schema cố định; tool xuất các đơn được nhận diện ra CSV.
8. Ghi phần nội dung chữ vào Google Sheet theo ngày và tải `message_image` lên folder
   ảnh cùng ngày trong folder Drive đã cấu hình.

## Cài đặt

Yêu cầu Python 3.10 trở lên.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

Mặc định tool dùng Google Chrome đã cài trên máy (`BROWSER_CHANNEL=chrome`). Nếu máy
không có Chrome, chạy `python -m playwright install chromium` và đặt
`BROWSER_CHANNEL=` thành rỗng trong `.env`.

Mở `.env` và điền tối thiểu:

```dotenv
ZALO_GROUP_NAME=Tên chính xác của nhóm
GEMINI_API_KEY=api-key-của-bạn
```

### Cấu hình Google Drive output

1. Trong Google Cloud, bật **Google Drive API** và **Google Sheets API**. Có thể dùng
   OAuth Client loại **Web application** hoặc **Desktop app**.
   - Với Web Client, thêm Authorized redirect URI chính xác:
     `http://127.0.0.1:8766/`.
   - Desktop Client không cần cấu hình redirect URI.
2. Cấu hình `.env` (không commit file client hoặc token):

```dotenv
GOOGLE_DRIVE_UPLOAD_ENABLED=true
GOOGLE_DRIVE_PARENT_FOLDER_ID=16kKkK80VwV92uWwxaBklivhHhYArrY2G
GOOGLE_OAUTH_CLIENT_SECRET_FILE=/duong-dan-tuyet-doi/client-secret.json
GOOGLE_OAUTH_TOKEN_FILE=.google-drive-token.json
GOOGLE_OAUTH_REDIRECT_PORT=8766
```

Lần chạy đầu sẽ mở trình duyệt để đăng nhập Google và cấp quyền; các lần sau dùng
token cục bộ. Ngoài ra có thể dùng Application Default Credentials qua
`GOOGLE_APPLICATION_CREDENTIALS`. Nếu dùng service account, folder phải được chia
sẻ quyền Editor cho `client_email`; Shared Drive phù hợp hơn My Drive vì giới hạn
quyền sở hữu/dung lượng của service account.

Tên Sheet/folder lấy theo ngày được chọn để crawl, không lấy theo giờ hệ thống khi
chạy lại dữ liệu cũ. Nếu chạy lại cùng nhóm và ngày, tool dùng lại tài nguyên đã tạo,
không thêm lại tin nhắn/ảnh có cùng định danh. Có thể đặt
`GOOGLE_DRIVE_UPLOAD_ENABLED=false` để chỉ lưu output cục bộ.

Không đưa `.env` lên Git. `.env`, hồ sơ trình duyệt, cache và dữ liệu đầu ra đã được
thêm vào `.gitignore`.

## Chạy theo cách khuyến nghị

Chế độ `persistent` mở một Chromium riêng và lưu cookie tại `.browser-profile`:

```bash
zalo-order-crawler run --group "Tên chính xác của nhóm" --date today
```

Lần đầu, đăng nhập Zalo trong cửa sổ Chromium vừa mở. Tool chờ tối đa 3 phút; những
lần sau hồ sơ này vẫn giữ phiên đăng nhập. Có thể crawl và làm sạch trước, chưa gọi AI:

```bash
zalo-order-crawler crawl --group "Tên chính xác của nhóm" --date 28/08/2026
```

Nếu hồ sơ Playwright hiện nút **Đồng bộ ngay**, tool sẽ dừng mặc định vì Zalo có thể
tải lịch sử rộng hơn ngày đang crawl. Chỉ đặt `ALLOW_ZALO_HISTORY_SYNC=true` sau khi
chủ tài khoản đồng ý với phạm vi đồng bộ đó.

Sau đó chạy lại riêng bước Gemini:

```bash
zalo-order-crawler classify --input output/Ten-nhom/2026-08-28/120000/clean_messages.jsonl
```

## Kết nối trình duyệt đang mở qua CDP

Playwright không thể gắn vào một Chrome/Edge bình thường đã mở mà không có cổng điều
khiển. Trình duyệt phải được khởi động với `--remote-debugging-port=9222`. Nên dùng
một `user-data-dir` riêng để tránh xung đột hoặc làm hỏng hồ sơ Chrome chính.

Linux:

```bash
google-chrome --remote-debugging-port=9222 \
  --user-data-dir="$PWD/.chrome-cdp-profile" https://chat.zalo.me/
```

Windows PowerShell (điều chỉnh đường dẫn Chrome nếu cần):

```powershell
& 'C:\Program Files\Google\Chrome\Application\chrome.exe' `
  --remote-debugging-port=9222 `
  --user-data-dir="$PWD\.chrome-cdp-profile" `
  https://chat.zalo.me/
```

Đăng nhập trong cửa sổ đó, đặt `BROWSER_MODE=cdp` trong `.env`, rồi chạy:

```bash
zalo-order-crawler run --browser-mode cdp --group "Tên nhóm" --date today
```

Tool chỉ ngắt kết nối khi xong, không chủ động đóng trình duyệt ở chế độ CDP.

## Dữ liệu đầu ra

Mỗi lần chạy tạo một thư mục:

```text
output/<ten-nhom>/<YYYY-MM-DD>/<HHMMSS>/
├── assets/                   # Ảnh tin nhắn và thumbnail đã tải về
├── raw_messages.jsonl       # HTML thô của từng node tin nhắn
├── raw_messages.html        # Snapshot xem cục bộ, có Content-Security-Policy
├── message_view.html        # HTML khung tin nhắn, không gồm sidebar hội thoại
├── stylesheets.json         # URL stylesheet và CSS đọc được từ trình duyệt
├── clean_messages.jsonl     # Nội dung BeautifulSoup đã làm sạch
├── classifications.jsonl    # Quyết định AI cho mọi tin nhắn
├── orders.csv               # Chỉ các tin được đánh dấu là đơn hàng
└── manifest.json             # Thống kê và cảnh báo của lần chạy
```

`orders.csv` dùng UTF-8 BOM để Excel trên Windows đọc tiếng Việt đúng.

## Khi giao diện Zalo thay đổi

Selector mặc định nằm tại `config/selectors.json`. Tool ưu tiên nhãn/thuộc tính hiển
thị và có nhiều selector dự phòng. Nếu không xác định chắc chắn nút hoặc nhóm, tool
dừng và ghi ảnh cùng HTML vào `debug/<thoi-gian>/` thay vì bấm tiếp. Cập nhật selector
dựa trên DOM thực tế rồi chạy lại.

Các file trong `debug/`, `output/` và `.browser-profile/` có thể chứa dữ liệu riêng tư.
Không gửi hoặc tải chúng lên nơi công cộng khi chưa xóa thông tin nhạy cảm.

## Quyền riêng tư và giới hạn

- Tool không yêu cầu hoặc ghi lại mật khẩu Zalo. Cookie đăng nhập vẫn được trình
  duyệt giữ cục bộ trong hồ sơ riêng.
- Gemini nhận nội dung tin nhắn đã làm sạch và ảnh đính kèm của tin nhắn. Dữ liệu này
  có thể gồm số điện thoại, địa chỉ, hình ảnh và dữ liệu khách hàng; chỉ sử dụng khi
  bạn có quyền xử lý và chia sẻ dữ liệu đó với nhà cung cấp AI.
- Việc nhận diện đơn hàng là đánh giá bằng mô hình, không phải xác nhận tuyệt đối.
  Nên duyệt các dòng confidence thấp trước khi nhập vào hệ thống bán hàng.
- Chỉ crawl nhóm và dữ liệu bạn được phép truy cập; tuân thủ điều khoản của Zalo và
  quy định bảo vệ dữ liệu áp dụng cho hoạt động của bạn.
