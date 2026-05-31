import socket
import time

# UNO R4 WiFi IP
R4_IP = "192.168.1.90"
PORT = 5050


def send_car(cmd: str):
    """
    Sends one command to the UNO R4 WiFi car server.

    Commands:
    F = forward
    B = backward
    L = left
    R = right
    S = stop
    D = distract mode
    """
    cmd = cmd.strip().upper()

    if cmd not in ["F", "B", "L", "R", "S", "D"]:
        raise ValueError(f"Invalid car command: {cmd}")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(3)
        s.connect((R4_IP, PORT))
        s.sendall(cmd.encode("ascii"))

        response = s.recv(1024).decode(errors="ignore").strip()
        print(f"Car command {cmd}: {response}")
        return response


def stop_car():
    send_car("S")


def forward(seconds=0.8):
    send_car("F")
    time.sleep(seconds)
    send_car("S")


def backward(seconds=0.8):
    send_car("B")
    time.sleep(seconds)
    send_car("S")


def turn_left(seconds=0.4):
    send_car("L")
    time.sleep(seconds)
    send_car("S")


def turn_right(seconds=0.4):
    send_car("R")
    time.sleep(seconds)
    send_car("S")


def distract():
    send_car("D")


def deploy_decoy():
    """
    Main 3Ds DISTRACT action.
    This is the function the AI pipeline should call.
    """
    print("Deploying 3Ds DISTRACT mode...")

    send_car("F")
    time.sleep(0.8)

    send_car("S")
    time.sleep(0.2)

    send_car("D")
    time.sleep(1.5)

    send_car("S")


if __name__ == "__main__":
    deploy_decoy()
