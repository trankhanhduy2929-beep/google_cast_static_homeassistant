# Google Cast Static IP cho Home Assistant

Custom integration này kết nối trực tiếp tới loa Google Cast bằng địa chỉ IPv4 và cổng `8009`. Nó không dùng bản ghi mDNS `_googlecast._tcp.local` hoặc hostname `.local`, nên phù hợp với lỗi kiểu:

```text
Failed to connect to service MDNSServiceInfo(...), retrying in 5.0s
```

Kết nối socket được giữ bởi PyChromecast và tự thử lại vô hạn khi Wi-Fi hoặc loa tạm thời mất kết nối.

Integration chấp nhận PyChromecast `14.0.9` đến trước `15.0`, tránh xung đột giữa các bản Home Assistant đang dùng `14.0.9` hoặc `14.0.10`.

## Chuẩn bị mạng

1. Đặt **DHCP reservation/IP tĩnh** cho Google Home Mini trên router.
2. Home Assistant phải truy cập được IP của loa qua TCP `8009`; bước thêm thiết bị cũng dùng `8008` hoặc `8443` để đọc thông tin thiết bị.
3. Để phát TTS/media từ Home Assistant, loa phải truy cập được URL nội bộ của Home Assistant. Nên cấu hình URL nội bộ bằng IP, ví dụ `http://192.168.1.10:8123`, thay vì hostname `.local`.

## Cài đặt thủ công

Sao chép thư mục:

```text
custom_components/google_cast_static
```

vào:

```text
/config/custom_components/google_cast_static
```

Sau đó khởi động lại Home Assistant.

## Thêm loa

1. Vào **Settings → Devices & services → Add integration**.
2. Tìm **Google Cast Static IP**.
3. Nhập IPv4 của Google Home Mini, ví dụ `192.168.1.50`; giữ cổng mặc định `8009`.
4. Integration kiểm tra cổng Cast và lấy UUID/tên/model trực tiếp từ loa.

Nếu cổng `8009` truy cập được nhưng Home Assistant không đọc được thông tin qua `8008/8443`, nhập UUID dự phòng. Với log trong yêu cầu này, UUID là:

```text
19fdb31c-52ca-89c2-cc5a-8e8b7b7cf202
```

Nếu DHCP làm IP thay đổi, mở integration và chọn **Reconfigure** để nhập IP mới. UUID phải là cùng một loa.

## Tránh entity và lỗi trùng lặp

Integration Google Cast mặc định vẫn tiếp tục dùng mDNS. Nếu giữ nó hoạt động cho cùng chiếc loa, bạn vẫn có thể thấy log mDNS cũ và hai entity media player.

- Nếu chỉ dùng chiếc loa này: tắt integration **Google Cast** mặc định sau khi custom integration hoạt động.
- Nếu còn thiết bị Cast khác: trong tùy chọn Google Cast mặc định, chỉ khai báo UUID của các thiết bị khác để nó không tạo entity cho Google Home Mini này.

## Kiểm tra phát media

Đặt file `test.mp3` trong `/config/www/`, rồi gọi action:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.google_home_mini
data:
  media_content_id: "http://192.168.1.10:8123/local/test.mp3"
  media_content_type: "audio/mpeg"
```

Thay IP Home Assistant và entity ID bằng giá trị thực tế của bạn.

## Tùy chọn kết nối

- **Retry delay**: mặc định ban đầu `5` giây; PyChromecast tăng dần thời gian chờ sau nhiều lần lỗi, tối đa khoảng 5 phút.
- **Socket timeout**: mặc định `30` giây.

Trong trạng thái entity có các thuộc tính `connection_method`, `connection_status`, `host` và `port` để chẩn đoán.
