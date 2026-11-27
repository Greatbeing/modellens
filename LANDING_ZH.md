# ModelLens —— 「看清你的模型到底哪个能用」

面向 MCP 的模型能力探测与路由工具。5 个工具，按次计费，无订阅、无账号。

托管端点：https://8000-agqz7vbdiiy6cc2n2tbs63iji4.instabox.tech/mcp  
x402 清单：https://8000-agqz7vbdiiy6cc2n2tbs63iji4.instabox.tech/.well-known/x402

---

## 「你可能遇到的问题」

某网关宣称拥有 89 个模型，但默认路由组把一个响应耗时 87 秒的模型排在第一，0.9 秒的模型却只排在第三。ModelLens 一次探测就把这个问题揪了出来——哪些模型真能用，哪些只是「宣称」能用，一探便知。不要让用户的等待成本替你付账。

---

## 功能表

| 工具 | 返回内容 | 价格（x402，USDC/Base） |
| --- | --- | --- |
| list_models | 列出可用模型清单与基础元信息 | 免费 |
| ranking | 按探测指标（延迟、成功率等）对模型排序 | 0.01 美元/次 |
| recommend | 根据使用场景推荐最适合的模型 | 0.01 美元/次 |
| report | 生成模型能力与可用性报告 | 0.02 美元/次 |
| probe | 单次模型探测，测延迟/可用性，发现异常路由 | 0.05 美元/次 |

---

## 「怎么打分」

每次探测都是一次真实请求，所以分数是测出来的，不是估出来的。

**一句话：**

```math
\text{得分} = 0.5 \times \text{成功} + 0.3 \times \text{速度} + 0.2 \times \text{稳定}
```

每一项都是 0–100 分：

| 项 | 权重 | 看什么 | 怎么算 |
|---|---|---|---|
| 🎯 **成功** | 50% | 能不能答上来 | 成功率，**样本少会打折**（见下） |
| ⚡ **速度** | 30% | 快不快 | `100 × (1 − 中位延迟 / 8 秒)`：0 秒 → 100，4 秒 → 50，≥ 8 秒 → 0 |
| 🛡️ **稳定** | 20% | 会不会卡住、被中断 | `100 − 2 × 取消率`：0% → 100，50% → 0 |

成功率为 0 的模型**直接 0 分**，再快也没用。

#### 为什么样本少要打折？

答对一次不算数。ModelLens 用 Wilson 95% 置信下界计算成功率，意思是“有 95% 把握它至少能达到的成功率”。探测次数越少，折扣越大：

| 成功次数 | 原始成功率 | 实际按 |
|---|---|---|
| 1 次中 1 次 | 100% | **21%** |
| 5 次中 5 次 | 100% | **57%** |
| 20 次中 20 次 | 100% | **84%** |
| 40 次中 40 次 | 100% | **91%** |

<details>
<summary>完整公式（感兴趣再看）</summary>

$n$ 次探测中成功 $k$ 次，$\hat p = k/n$，$z = 1.96$：

```math
\text{成功} = 100 \times \frac{\hat p + \frac{z^2}{2n} - z\sqrt{\frac{\hat p(1-\hat p)}{n} + \frac{z^2}{4n^2}}}{1 + \frac{z^2}{n}}
```

</details>

#### 算例

| | 侥幸模型 | 久经考验的模型 |
|---|---|---|
| 证据 | 1 次中 1 次，150 毫秒 | 40 次中 40 次，400 毫秒 |
| 成功 × 0.5 | 21 × 0.5 = 10.3 | 91 × 0.5 = 45.6 |
| 速度 × 0.3 | 98 × 0.3 = 29.4 | 95 × 0.3 = 28.5 |
| 稳定 × 0.2 | 100 × 0.2 = 20.0 | 100 × 0.2 = 20.0 |
| **得分** | **59.8** | **94.1** |

侥幸模型更快，但排第一的是久经考验的那个。探测不足 5 次的模型还会标上 `low_evidence`。

---

## 「怎么计费」

采用 x402 按次付费，**无订阅、无账号**，每次只为实际使用付费：

1. **调用**：客户端请求任意付费工具（ranking / recommend / report / probe）
2. **402 challenge**：服务端返回 402 Payment Required，附带支付说明
3. **签名**：用 EIP-3009 签署 USDC(Base) 支付意向
4. **结算**：facilitator 完成结算，你拿到结果 + 收据

结算链：USDC / Base 网络。

---

## 快速上手

**Python 调用示例（10 行以内）：**

```python
import requests

ENDPOINT = "https://8000-agqz7vbdiiy6cc2n2tbs63iji4.instabox.tech/mcp"
resp = requests.post(ENDPOINT, json={
    "tool": "ranking",
    "arguments": {"models": ["gpt-4o", "claude-3-opus", "qwen-max"]}
})
print(resp.json())
```

**MCP 客户端配置示例：**

```json
{
  "mcpServers": {
    "modellens": {
      "command": "mcp-remote",
      "args": ["https://8000-agqz7vbdiiy6cc2n2tbs63iji4.instabox.tech/mcp"]
    }
  }
}
```

---

## 自托管方案

```bash
pip install modellens-mcp
```

MCP Registry：io.github.Greatbeing/modellens

MCP 客户端配置示例（自托管）：

```json
{
  "mcpServers": {
    "modellens": {
      "command": "python",
      "args": ["-m", "modellens_mcp.server"]
    }
  }
}
```

> 根据 `modellens-mcp` 包提供的实际入口调整 `args`，或使用该包附带的 CLI 命令进行配置。

---

## 常见问题

**Q: 要订阅吗？**  
不需要。全部按次付费（x402），无订阅、无账号；list_models 永久免费。

**Q: 支持哪些链？**  
Base 网络上的 USDC，使用 EIP-3009 签名，由 facilitator 完成结算。

**Q: 后端挂了怎么办？**  
托管端点由 ModelLens 运维维护。如需自主可控，可 `pip install modellens-mcp` 自行部署，探测、路由决策和数据全部在你自己的环境中运行。

**Q: 怎么私有化部署？**  
`pip install modellens-mcp` 后，按 MCP Registry `io.github.Greatbeing/modellens` 的说明配置即可，接入你自己的 MCP 客户端，无需依赖公共托管端点。

---

*ModelLens —— 看清你的模型到底哪个能用。*
