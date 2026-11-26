# 转运车损的共同查勘与保险追偿

多段运输查勘、事故保单额度和实际支付对账。

`samples/` 保存原始样本和字段约定，《交换资料约定》说明同一对象的编号、时间及来源。`claims/` 是理赔协同服务：查勘员、承运人和财务在同一宗案件里继续处理自己的决定。

## 运行环境

使用 Python 3.11.2。资料检查与服务均使用标准库，无需下载安装包。由项目根目录运行以下命令，可以检查合成资料能否解析、引用能否对应以及业务规则是否成立。

```sh
python3 -m unittest discover -s tests -v
```

## 理赔协同服务

```sh
python3 claims_server.py --port 8090 --peer http://127.0.0.1:8087 --seed samples --state state.json
```

`--peer` 是付款对手接口地址，`--seed` 可选载入样本资料，`--state` 可选保存状态快照。`python3 demo_case.py` 在本地同时启动付款对手重放与本服务，演示一宗案件的完整协同。

请求头用 `X-Role` 区分角色：`surveyor`（查勘）、`party`（承运人，须同时给 `X-Party`）、`manager`（理赔经理）、`finance`（财务）。承运人只能看到自己经手区间及责任和证据，理赔经理可以查整案。

### 交接与证据

- `POST /checks` 查勘员或有区间证据的交接方提交车架号、交接区间、损伤部位和查勘时点；晚上传的证据按实际查勘时点 `at` 解释，与提交先后无关。
- `POST /checks/{check}/ack` 交接双方确认（`confirm`）或异议（`object`，须说明理由）；异议即记争议，双方可查看并补交自己的区间证据，服务不猜定责任人。
- `POST /incidents` 查勘机构确认共同事故编号；一车多处受损可各自关联不同事故，一次事故也可涉及多辆车。
- `POST /reports` 提交车损报告；同部位第二次受损引用各自的查勘记录，独立申请和确认，不并案。
- `GET /reports/{report}` 按查勘时点解释：当时交接是否已有该伤（`damage_present_at_prior`）以及哪几段还可能担责（`candidate_legs`）。
- `POST /reports/{report}/incident` 共同事故归属改变：未付款承诺进入复核并停止占用原认定，已付款垫款保留对账、不按新归属重复支付。

### 垫款审批

- `POST /claims` 维修清单逐车报价，同一清单的报告须属于同一共同事故。
- `POST /claims/{claim}/approve` 理赔经理批准后构成可执行承诺；累计占用不得超过该事故可赔总额（同事故共用一份免赔额）以及整份保单的剩余额度（上限跨事故共用）。
- 待审申请经 `GET /claims/{claim}` 看得到余量不足的原因；`POST /claims/{claim}/review` 复核归属调整后的承诺；因额度收紧造成的不足列入 `GET /policies/{policy}` 的 `followups`，作为可解释的后续处理。

### 付款、追偿与对账

- `POST /payments` 按已批准的承诺支付修理垫款；`POST /recoveries` 发起追偿，只依据双方已经认可或复核完毕的责任份额。两者分别计数。
- 同一笔付款提交后失去应答时状态为 `unknown`，`POST /transfers/{ref}/reconcile` 先查该笔是否已支付，不列为可重新付款；对端未收到（`not_found`）才可按原内容重新提交。
- 结算（`settled`）的修理付款计入实际垫付，结算的追偿才计入回款；`GET /transfers/{ref}` 查看单笔状态。

### 案件查询

- `GET /vehicles/{vin}/case` 按一辆车查清证据、实际垫付、待追偿金额和未决事项。
- `GET /incidents/{incident}/case` 按一次事故查清同样内容（可涉及多辆车）。
- `GET /policies/{policy}` 查看保单上限、各事故可赔总额与占用、剩余额度和后续处理项。

## 本地对端

运行 `python3 peer_replay.py` 可在 `127.0.0.1:8087` 重放 `samples/peer.json` 中的 HTTP 录制。`--recording` 可读取另一份同格式录制，`--port` 可更改端口。每条录制依次写请求方法、路径、JSON 请求体和响应序列。没有请求体时写 null。每种请求独立推进响应，最后一项可重复读取，状态码 0 代表连接断开且未收到应答。重启重放进程会重新开始读取录制，不代表现实中的远端状态被重置。
