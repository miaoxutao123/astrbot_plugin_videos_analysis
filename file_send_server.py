import asyncio
import os
import struct

import aiofiles
import aiofiles.os


async def send_file(filename, HOST, PORT):
    try:
        reader, writer = await asyncio.open_connection(HOST, PORT)
        file_name = os.path.basename(filename)
        file_name_bytes = file_name.encode("utf-8")

        # 发送文件名长度和文件名
        writer.write(struct.pack(">I", len(file_name_bytes)))
        writer.write(file_name_bytes)

        # 发送文件大小
        file_size = await aiofiles.os.stat(filename)
        writer.write(struct.pack(">Q", file_size.st_size))

        # 发送文件内容
        async with aiofiles.open(filename, "rb") as f:
            while True:
                data = await f.read(4096)
                if not data:
                    break
                writer.write(data)
        await writer.drain()
        print(f"文件 {file_name} 发送成功")

        # 接收接收端发送的文件绝对路径
        file_abs_path_len_data = await recv_all(reader, 4)
        if not file_abs_path_len_data:
            print("无法接收文件绝对路径长度")
            return
        file_abs_path_len = struct.unpack(">I", file_abs_path_len_data)[0]

        file_abs_path_data = await recv_all(reader, file_abs_path_len)
        if not file_abs_path_data:
            print("无法接收文件绝对路径")
            return
        file_abs_path = file_abs_path_data.decode("utf-8")
        print(f"接收端文件绝对路径: {file_abs_path}")
        return file_abs_path
    except Exception as e:
        print(f"传输失败: {e}")

async def recv_all(reader, n):
    data = bytearray()
    while len(data) < n:
        packet = await reader.read(n - len(data))
        if not packet:
            return None
        data.extend(packet)
    return data

# if __name__ == "__main__":
#     file_path = input("请输入要发送的文件路径: ")
#     if os.path.isfile(file_path):
#         asyncio.run(send_file(file_path, 'localhost', 3658))
#     else:
#         print("错误: 文件不存在")
