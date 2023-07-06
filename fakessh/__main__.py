from .server import Server


def main():
    server = Server(command_handler=lambda c: c, port=5050)

    try:
        server.run_blocking()
    except KeyboardInterrupt:
        server.close()


if __name__ == '__main__':
    main()
