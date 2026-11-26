"""理赔协同服务入口。

查勘员、承运人和财务通过本服务在同一宗案件里提交交接、理赔及追偿操作。
付款对手（如 `python3 peer_replay.py` 重放的合成对端）按项目约定回报汇款和追偿结果。
"""

import argparse
import json
from pathlib import Path

from claims import ClaimService, PeerClient, create_server, seed_from_samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8090, help="仅监听回环地址")
    parser.add_argument("--peer", default="http://127.0.0.1:8087", help="付款对手接口地址")
    parser.add_argument("--seed", metavar="目录", help="可选：从样本目录载入合成资料")
    parser.add_argument("--state", metavar="文件", help="可选：状态快照文件，启动时载入、变更时保存")
    options = parser.parse_args()

    peer = PeerClient(options.peer)
    if options.state and Path(options.state).exists():
        state = json.loads(Path(options.state).read_text(encoding="utf-8"))
        service = ClaimService.from_snapshot(state, peer=peer)
    else:
        service = ClaimService(peer=peer)
    if options.seed:
        seed_from_samples(service, options.seed)

    server = create_server(service=service, port=options.port, state_path=options.state)
    print(f"理赔协同服务监听 127.0.0.1:{server.server_port}，付款对手 {options.peer}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
