
import socket
import time
import csv

HOST = "10.0.0.2"
PORT = 5000

sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sender.settimeout(2)

sequence = 1
x = 10
y = 5
previous_rtt = None

file = open("measurements.csv", "w", newline="")
writer = csv.writer(file)

writer.writerow([
    "sequence",
    "x",
    "y",
    "rtt_ms",
    "rtt_variation_ms",
    "reply_received"
])

print("Player 1 is running!")
print("Saving measurements to measurements.csv")

try:
    while True:
        message = f"player=1, sequence={sequence}, x={x}, y={y}"
        data = message.encode()

        start = time.perf_counter()
        sender.sendto(data, (HOST, PORT))

        rtt = ""
        variation = ""
        reply_received = 0

        try:
            reply, address = sender.recvfrom(1024)
            end = time.perf_counter()

            rtt = (end - start) * 1000
            reply_received = 1

            print(f"Sequence: {sequence}")
            print(f"RTT: {rtt:.3f} ms")

            if previous_rtt is not None:
                variation = abs(rtt - previous_rtt)
                print(f"RTT variation: {variation:.3f} ms")

            previous_rtt = rtt

        except socket.timeout:
            print(f"Sequence {sequence}: No reply received")

        writer.writerow([
            sequence,
            x,
            y,
            rtt,
            variation,
            reply_received
        ])
        file.flush()

        sequence += 1
        x += 1
        y += 1

        time.sleep(1)

except KeyboardInterrupt:
    print("\nPlayer 1 stopped.")

finally:
    sender.close()
    file.close()

