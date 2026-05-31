import socket
import time

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 5050

def send_car(cmd: str):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((BRIDGE_HOST, BRIDGE_PORT))
        s.sendall(cmd.encode("ascii"))
        response = s.recv(1024).decode(errors="ignore").strip()
        print(f"Car command {cmd}: {response}")
        return response

def stop_car():
    send_car("S")

def deploy_decoy():
    print("Deploying 3Ds DISTRACT mode")

    send_car("F")
    time.sleep(0.8)

    send_car("S")
    time.sleep(0.2)

    send_car("D")
    time.sleep(1.5)

    send_car("S")