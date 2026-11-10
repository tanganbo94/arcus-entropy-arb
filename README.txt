Arcus × Entropy 永续工具（本机运行）

密钥只保存在本机的 API.txt（或 .env），不要把私钥、助记词或 API Signing Key
发到聊天里，也不要上传到网盘或提交到 git。

----------------------------------------------------------------
一、安装依赖（只需做一次）

在本文件夹打开命令行，执行：

    py -m pip install -r requirements.txt

----------------------------------------------------------------
二、只读比价（scan.py，不需要密钥，也不会下单）

    py scan.py
    或：
    python scan.py

每 15 秒刷新：

    py scan.py --watch 15

----------------------------------------------------------------
三、账户状态与确认下单（trade.py）

先把网页里生成的「只能交易、不能提币」的钥匙写入本文件夹的 API.txt，
格式如下（不要把填好的真实文件发给任何人）：

    ARCUS_SIGNING_KEY=（64 位十六进制，不要 0x）
    ARCUS_ACCOUNT_INDEX=1
    ARCUS_WALLET_ADDRESS=0x你的钱包地址
    HL_API_WALLET_PRIVATE_KEY=0x（Hyperliquid API Wallet 私钥）
    HL_ACCOUNT_ADDRESS=0x你的主钱包地址

查询账户时必须用主钱包地址（HL_ACCOUNT_ADDRESS），不要用 API Wallet 的地址，
否则会看到空账户。ARCUS_ACCOUNT_INDEX 必须与授权时选择的子账户一致。

默认命令只查状态，不会下单：

    py trade.py
    或：
    py trade.py --status

查看某个同名永续的两边价格与估算价差（不下单）：

    py trade.py --quote BTC

预览将要发送的两腿订单，不真正下单：

    py trade.py --dry-run --long arcus --short hl --coin BTC --size 0.001

加上 --price 则两边都按该限价挂单；省略时按标记价 ±5% 的保护限价，
以 IOC / 市价方式发送（Arcus 的市价必须带保护价，且偏离不得超过 10%）。

真正下单必须同时加上 --live 和 --i-confirm，并且应先在对话里确认：

    py trade.py --live --i-confirm --long arcus --short hl --coin BTC --size 0.001

只有 --live 而没有 --i-confirm 会被拒绝，不会发单。
同一个标的如果在 Hyperliquid 多个盘口（主站 / xyz / io 等）都有合约，
脚本不会擅自猜测，请写成例如 xyz:BTC 再预览或下单。

======== 本地循环（推荐，不走 Bot）========
在这台电脑上跑，行情和下单只在本机和交易所之间传输，不必开着 Bot 远程操作。

实盘循环（按已约定规则：约 2000U、有边才下、单腿立刻平、按价差止盈止损）：
  双击 start_watch.bat
  或在本目录执行：  py watch.py

只看机会、不下单：
  py watch.py --dry-run

只跑一轮：
  py watch.py --once

运行中不要关闭窗口，按 Ctrl+C 停止。日志在 logs 文件夹，持仓记录在 hedge_state.json。
API.txt 只留在本机。仓库里已有 .gitignore，上传 GitHub 时不会带上密钥、日志和持仓记录。
