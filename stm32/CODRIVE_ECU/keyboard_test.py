
import serial

PORT = "COM7"  # 실제 ST-LINK Virtual COM Port 번호
BAUD = 115200

with serial.Serial(PORT, BAUD, timeout=1) as ser:
    print("1 = LED ON")
    print("0 = LED OFF")
    print("T = LED Toggle")
    print("Q = Quit")

    while True:
        key = input("입력 > ").strip()

        if key.lower() == "q":
            break

        if key.lower() in ("1", "0", "t"):
            ser.write(key.encode("ascii"))
            print("전송 완료:", key)
