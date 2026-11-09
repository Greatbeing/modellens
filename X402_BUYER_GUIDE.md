# ModelLens Buyer Integration Guide — x402 (USDC on Base)

Pay for ModelLens's hosted MCP tools with x402: USDC on Base, pay-per-call, no subscriptions.

**Endpoint:** `https://8000-agqz7vbdiiy6cc2n2tbs63iji4.instabox.tech/mcp`
**Manifest:** `GET https://8000-agqz7vbdiiy6cc2n2tbs63iji4.instabox.tech/.well-known/x402`

## 1. Overview

x402 is an HTTP payment protocol: when a paid API is called without payment, the server returns HTTP 402 with a signed payment challenge; the client signs an authorization (here, EIP-3009) and retries, and the server verifies + settles via the x402 facilitator (`https://x402.org/facilitator`). Settlement is USDC on Base (`eip155:8453`), so each tool call costs atomic USDC (6 decimals) instead of a monthly fee. Per-call pricing beats subscriptions for agents: an agent only pays for the tools it actually invokes, there are no idle-seat costs, and spending is bounded per request rather than per month.

## 2. The challenge (HTTP 402)

Paid ModelLens tools (`ranking`, `recommend`, `report`, `probe`; `list_models` is free at 0) return:

```
HTTP 402
PAYMENT-REQUIRED: <base64url(JSON challenge)>
```

The challenge is a JSON object with exactly these fields. Filled in for the **ranking** tool (price 10000 atomic USDC):

```json
{
  "scheme": "exact",
  "network": "eip155:8453",
  "maxAmountRequired": "10000",
  "resource": "https://8000-agqz7vbdiiy6cc2n2tbs63iji4.instabox.tech/mcp",
  "description": "ranking",
  "payTo": "0x2aB8D0071b8D9d3Ac194ba10a3f8767522920d50",
  "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
  "extra": {}
}
```

Field meanings:

| Field | Value for `ranking` | Meaning |
|---|---|---|
| `scheme` | `"exact"` | Exact-amount payment scheme |
| `network` | `"eip155:8453"` | Base mainnet |
| `maxAmountRequired` | `"10000"` | 0.00001 USDC (atomic, 6 decimals) |
| `resource` | MCP endpoint URL | What is being paid for |
| `description` | `"ranking"` | Tool name |
| `payTo` | `0x2aB8D0071b8D9d3Ac194ba10a3f8767522920d50` | ModelLens payout address |
| `asset` | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | USDC on Base |
| `extra` | `{}` | Opaque facilitator metadata |

Prices (atomic USDC, 6 decimals): `list_models` 0, `ranking` 10000, `recommend` 10000, `report` 20000, `probe` 50000.

## 3. EIP-3009 authorization

To pay, sign an ERC-3009 `TransferWithAuthorization` typed-data message authorizing USDC transfer from your wallet to `payTo`. You need a USDC-funded wallet on Base (`eip155:8453`) — hold USDC at `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` plus a little ETH for gas.

Typed-data structure:

**Domain**
```json
{
  "name": "USDC",
  "version": "2",
  "chainId": 8453,
  "verifyingContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
}
```

**Types**
```
TransferWithAuthorization(
  address from,
  address to,
  uint256 value,
  uint256 validAfter,
  uint256 validBefore,
  bytes32 nonce
)
```

**Message** (example values)
```json
{
  "from": "0xYOUR_WALLET_ADDRESS",
  "to": "0x2aB8D0071b8D9d3Ac194ba10a3f8767522920d50",
  "value": "10000",
  "validAfter": 1700000000,
  "validBefore": 1700003600,
  "nonce": "0x<32 random bytes>"
}
```

The retry header is `X-PAYMENT: <base64url(JSON auth payload)>` where the payload is the message above plus a `signature` field containing your signed EIP-3009 signature (0x-prefixed hex). On success the server answers HTTP 200 with `PAYMENT-RESPONSE: <base64url(receipt)>`.

## 4. Step-by-step flow

1. **Call the tool.** POST JSON-RPC to `https://8000-agqz7vbdiiy6cc2n2tbs63iji4.instabox.tech/mcp` (e.g. `{"jsonrpc":"2.0","method":"tools/call","params":{"name":"ranking","arguments":{...}},"id":1}`).
2. **Receive 402.** If the tool is paid and you haven't paid, the server returns HTTP 402 with header `PAYMENT-REQUIRED: <base64url(challenge)>`.
3. **Decode the challenge.** base64url-decode the header, parse JSON, read `maxAmountRequired`, `payTo`, `asset`, `network` (confirm `eip155:8453`).
4. **Build + sign authorization.** Construct the EIP-3009 `TransferWithAuthorization` message (value = `maxAmountRequired`, fresh random `bytes32` nonce, short validity window), sign it with your Base wallet via `signTypedData`.
5. **Retry with payment.** Re-send the identical request adding header `X-PAYMENT: <base64url({...message, signature})>`.
6. **Get the result.** On success the server returns HTTP 200 plus header `PAYMENT-RESPONSE: <base64url(receipt)>; read the response body for the tool output.

## 5. TypeScript sample (ethers v6)

```ts
import { ethers } from "ethers";

const ENDPOINT = "https://8000-agqz7vbdiiy6cc2n2tbs63iji4.instabox.tech/mcp";
const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const PAY_TO = "0x2aB8D0071b8D9d3Ac194ba10a3f8767522920d50";

export async function callPaidTool(tool: string, args: unknown, signer: ethers.Wallet): Promise<string> {
  const body = JSON.stringify({ jsonrpc: "2.0", method: "tools/call", params: { name: tool, arguments: args }, id: 1 });
  let res = await fetch(ENDPOINT, { method: "POST", headers: { "Content-Type": "application/json" }, body });
  if (res.status !== 402) return await res.text();

  const chal = JSON.parse(Buffer.from(res.headers.get("PAYMENT-REQUIRED")!, "base64url").toString());
  const now = Math.floor(Date.now() / 1000);
  const auth = {
    from: await signer.getAddress(),
    to: PAY_TO,
    value: BigInt(chal.maxAmountRequired),
    validAfter: now - 60,
    validBefore: now + 3600,
    nonce: ethers.randomBytes(32),
  };
  const sig = await signer.signTypedData(
    { name: "USDC", version: "2", chainId: 8453, verifyingContract: USDC },
    { TransferWithAuthorization: [{ name: "from", type: "address" }, { name: "to", type: "address" }, { name: "value", type: "uint256" }, { name: "validAfter", type: "uint256" }, { name: "validBefore", type: "uint256" }, { name: "nonce", type: "bytes32" }] },
    auth,
  );
  res = await fetch(ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-PAYMENT": Buffer.from(JSON.stringify({ ...auth, signature: sig })).toString("base64url") },
    body,
  });
  return await res.text();
}
```

Usage: `const out = await callPaidTool("ranking", { ... }, new ethers.Wallet(PRIVATE_KEY, new ethers.JsonRpcProvider("https://mainnet.base.org")));`

## 6. Python sample (eth-account)

```python
import base64, json, secrets, time, urllib.error, urllib.request
from eth_account import Account
from eth_account.messages import encode_typed_data

ENDPOINT = "https://8000-agqz7vbdiiy6cc2n2tbs63iji4.instabox.tech/mcp"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x2aB8D0071b8D9d3Ac194ba10a3f8767522920d50"
acct = Account.from_key("0xYOUR_PRIVATE_KEY")


def b64u(obj: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def call(tool: str, args: dict) -> str:
    body = json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": tool, "arguments": args}, "id": 1}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        if e.code != 402:
            raise
        chal = json.loads(base64.urlsafe_b64decode(e.headers["PAYMENT-REQUIRED"] + "=="))
        domain = {"name": "USDC", "version": "2", "chainId": 8453, "verifyingContract": USDC}
        types = {
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ]
        }
        now = int(time.time())
        value = {
            "from": acct.address,
            "to": PAY_TO,
            "value": int(chal["maxAmountRequired"]),
            "validAfter": now - 60,
            "validBefore": now + 3600,
            "nonce": secrets.token_bytes(32),
        }
        signed = Account.sign_message(
            encode_typed_data(domain=domain, message_types=types, message=value), acct.key
        )
        auth = {**value, "signature": signed.signature.hex()}
        req2 = urllib.request.Request(
            ENDPOINT, data=body, headers={"Content-Type": "application/json", "X-PAYMENT": b64u(auth)}
        )
        with urllib.request.urlopen(req2) as r:
            return r.read().decode()
```

## 7. Testnet vs mainnet

**Mainnet (this guide):** network `eip155:8453`, USDC at `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, payTo `0x2aB8D0071b8D9d3Ac194ba10a3f8767522920d50`. Costs real USDC.

**Testnet:** switch to Base Sepolia, network `eip155:84532`, bridge/mint USDC from a Base Sepolia faucet, and use the testnet USDC contract and payTo advertised by the testnet manifest (`GET /.well-known/x402` on the testnet deployment). Sign typed data with `chainId: 84532` and the testnet verifying contract.

**Why test first:** mainnet mistakes burn real USDC (wrong network, stale nonce, over-approval). Testnet lets you validate the 402 → decode → sign → retry loop end-to-end through the facilitator at no cost before switching the chainId to `8453`.

## 8. Troubleshooting

1. **"invalid payment"** — The signature didn't verify or the nonce was already used. Re-sign with a fresh random `bytes32` nonce and check you signed the exact domain (chainId 8453, USDC verifying contract) and message fields.
2. **Expired challenge** — The 402 challenge or your authorization's `validBefore` has passed (or clock skew). Re-request to get a fresh challenge and set `validAfter: now - 60`, `validBefore: now + 3600`.
3. **Insufficient USDC balance** — Your wallet holds less than `maxAmountRequired` USDC at `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, or lacks ETH for Base gas. Fund the wallet on Base and retry.
4. **Facilitator down** — `https://x402.org/facilitator` is unreachable, so verification/settlement stalls after your retry. Wait and retry; the 402 flow itself still works locally until settlement.
5. **Wrong network** — Signed on Ethereum mainnet (`eip155:1`) or Base Sepolia (`eip155:84532`) instead of Base (`eip155:8453`). Confirm `network` in the challenge and use chainId 8453 in the typed-data domain.
