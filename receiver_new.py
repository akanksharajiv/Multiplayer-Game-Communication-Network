

import socket

HOST = "0.0.0.0"
PORT = 5000

receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
receiver.bind((HOST, PORT))

print("Player 2 is ready!")
print("Waiting for game updates...")

try:
    while True:
        data, address = receiver.recvfrom(1024)
        message = data.decode()

        print("Received:", message)

        receiver.sendto(data, address)

except KeyboardInterrupt:
    print("\nReceiver stopped.")

finally:
    receiver.close()

