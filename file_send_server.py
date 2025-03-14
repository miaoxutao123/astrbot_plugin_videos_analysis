import socket
import os
import struct

def send_file(filename, HOST, PORT):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((HOST, PORT))
            file_name = os.path.basename(filename)
            file_name_bytes = file_name.encode('utf-8')
            
            # 发送文件名长度和文件名
            s.sendall(struct.pack('>I', len(file_name_bytes)))
            s.sendall(file_name_bytes)
            
            # 发送文件大小
            file_size = os.path.getsize(filename)
            s.sendall(struct.pack('>Q', file_size))
            
            # 发送文件内容
            with open(filename, 'rb') as f:
                while True:
                    data = f.read(4096)
                    if not data:
                        break
                    s.sendall(data)
            print(f"文件 {file_name} 发送成功")
            
            # 接收接收端发送的文件绝对路径
            file_abs_path_len_data = recv_all(s, 4)
            if not file_abs_path_len_data:
                print("无法接收文件绝对路径长度")
                return
            file_abs_path_len = struct.unpack('>I', file_abs_path_len_data)[0]
            
            file_abs_path_data = recv_all(s, file_abs_path_len)
            if not file_abs_path_data:
                print("无法接收文件绝对路径")
                return
            file_abs_path = file_abs_path_data.decode('utf-8')
            print(f"接收端文件绝对路径: {file_abs_path}")
            return file_abs_path
    except Exception as e:
        print(f"传输失败: {e}")

def recv_all(sock, n):
    data = bytearray()
    while len(data) < n:
        packet = sock.recv(n - len(data))
        if not packet:
            return None
        data.extend(packet)
    return data

# if __name__ == "__main__":
#     file_path = input("请输入要发送的文件路径: ")
#     if os.path.isfile(file_path):
#         send_file(file_path)
#     else:
#         print("错误: 文件不存在")