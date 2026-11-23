# Bộ script tự động hóa Python chuyển đổi từ file `.amk` (HSTC – Máy 4)

Các file `.amk` (Robot-Soft Automatic Mouse and Keyboard, định dạng văn bản UTF-16LE phiên bản 248) đã được chuyển đổi sang các script Python độc lập (`.py`). Mỗi file `.py` đã nhúng sẵn toàn bộ ảnh mẫu (dạng PNG lossless base64) và runtime Win32 bên trong, do đó có thể sao chép riêng từng file sang máy khác để chạy trực tiếp.

## Yêu cầu môi trường

- Hệ điều hành Windows (Python 3.10 trở lên).
- Độ phân giải màn hình khuyến nghị đúng theo máy gốc đã quay macro: `1920 × 1080` (tự động bật DPI-aware vật lý, hoạt động chính xác ngay cả khi Windows đặt scale `125%`).
- Thư viện cần cài đặt (chỉ cần chạy 1 lần trên máy mới): nhấp đúp `Cai_dat_thu_vien.bat` hoặc chạy lệnh:

```bash
pip install -r requirements.txt
```

## Cách chạy nhanh

### Cách 1: mở menu chọn script

Chạy lệnh sau trong thư mục `Python\`:

```bash
python run_menu.py
```

Trong menu:
- Chọn **`D`** để thay đổi đường dẫn file Excel database (hỗ trợ dán đường dẫn, bấm `b` mở hộp thoại chọn file Windows hoặc `reset` về tự động dò); cấu hình được lưu lâu dài vào `menu_config.json`.
- Chọn **`T`** để mở công cụ soi tọa độ (`F8` lấy điểm, `F9` đo độ lệch), kéo chọn vùng `region=(L, T, R, B)`, chụp lại ảnh mẫu `TEMPLATES['img_X']` (có tự động sao lưu vào `backups\`) hoặc xuất các ảnh mẫu ra `.png`.

### Cách 2: chạy trực tiếp từng script

```bash
python "0. Ky so full theo BS.py"
```

Các tùy chọn dòng lệnh được hỗ trợ trên tất cả script:

- `--dry-run`: chạy thử luồng xử lý mà không bấm chuột/phím thật, tự thoát vòng lặp vô hạn sau 1 lượt.
- `--start-delay 3`: chờ 3 giây trước khi bắt đầu để kịp chuyển sang cửa sổ phần mềm bệnh viện.
- `--excel "đường\dẫn\Database.xlsx"`: chỉ định file Excel dữ liệu (dành cho các script đọc `Database.xlsx`).
- `--delay-scale 1.2`: tăng hoặc giảm tốc độ trễ (ví dụ `1.2` chậm hơn 20%, `0.8` nhanh hơn 20%).
- `--verbose`: in thêm nhật ký độ trễ `sleep_ms` và từng ô Excel đã đọc.
- `--quiet`: tắt in nhật ký từng bước thực thi mặc định.

## Theo dõi vị trí lỗi & phím dừng khẩn cấp

- Mặc định khi chạy thật, mỗi thao tác đều in kèm số dòng trong file `.py` (ví dụ `[L076] mouse_move_abs(...)` hoặc `[func_ki:L087] find_image(...) -> THẤY img_3 (99.8%)`).
- Khi bấm **`F12`** (dừng khẩn cấp bất kỳ lúc nào), hết thời gian chờ ảnh/cửa sổ, hoặc gặp lỗi, chương trình tự nhả phím/chuột đang giữ và in rõ `[VỊ TRÍ DỪNG]` cùng mức độ khớp ảnh tốt nhất (`tốt nhất: img_X=...%`) để mở đúng dòng cần sửa.

## Lưu ý về dữ liệu nguồn

- **Hai file `.amk` nguồn trống:** `PEX.amk` và `ky so to dieu tri ( tu tren xuong duoi).amk` (đều có dung lượng 480 byte) chỉ chứa khối mẫu mặc định `# Add some commands here.` không có lệnh thực thi; hai file `.py` tương ứng giữ nguyên khung hàm `func_entry` với `pass`.
- **Các script sử dụng `Database.xlsx`:** `Chi dinh thu thuat.py`, `TUONG TRINH CHUAN.py` và `TUONG TRINH THO MAY.py` tự động tìm `Database.xlsx` ở các vị trí theo thứ tự: tham số `--excel` → đường dẫn gốc `Desktop\Database.xlsx` → cùng thư mục với script → các thư mục cha (tối đa 3 cấp, tự khớp `Crack\Database.xlsx` khi chạy từ `May 4\Python\`).
- **Tài nguyên bị vô hiệu hóa (`DIS: 1`) trong file `.amk`:** được giữ lại dưới dạng chú thích `# [DISABLED]` trong mã Python để dễ đối chiếu hoặc bật lại khi cần.

## Chuyển đổi lại khi có file `.amk` mới

Sử dụng công cụ `amk2py.py`:

```bash
python amk2py.py "đường\dẫn\thư_mục_amk" -o "đường\dẫn\thư_mục_Python"
```
