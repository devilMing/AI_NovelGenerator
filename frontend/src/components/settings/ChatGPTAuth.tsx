"use client";

import { useEffect, useState } from "react";
import { useLocale } from "next-intl";
import { Button } from "@heroui/react";
import { apiGet, apiPost } from "@/lib/api";
import type { ProviderConfig } from "@/types/config";
import { getChatGPTReasoningProfile } from "@/lib/chatgptReasoning";

type Account = { id: string; label: string; connected: boolean; plan_enabled: boolean };
type AuthStatus = { accounts: Account[]; status: string; message: string };

export function ChatGPTAuth({ provider, onChange }: {
  provider: ProviderConfig; onChange: (patch: Partial<ProviderConfig>) => void;
}) {
  const zh = useLocale() === "zh";
  const [auth, setAuth] = useState<AuthStatus>({ accounts: [], status: "idle", message: "" });
  const [models, setModels] = useState<{ id: string; name: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [loadingModels, setLoadingModels] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const enabled = provider.auth_mode === "chatgpt";
  const selected = auth.accounts.find((a) => a.id === provider.chatgpt_account_id);
  const reasoning = getChatGPTReasoningProfile(provider.default_model);
  const effortNames = zh ? { none: "不推理", minimal: "极低", low: "低", medium: "中等", high: "高", xhigh: "超高", max: "最高" } : { none: "None", minimal: "Minimal", low: "Low", medium: "Medium", high: "High", xhigh: "Extra high", max: "Maximum" };
  const modelOptions = provider.default_model && !models.some((m) => m.id === provider.default_model)
    ? [{ id: provider.default_model, name: provider.default_model }, ...models] : models;
  const selectClass = "w-full min-h-11 rounded-xl border border-border bg-background px-3 text-sm text-foreground";

  async function refresh() {
    try { setAuth(await apiGet<AuthStatus>("/api/config/chatgpt/status")); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }

  useEffect(() => {
    let active = true;
    apiGet<AuthStatus>("/api/config/chatgpt/status").then((value) => { if (active) setAuth(value); })
      .catch((e) => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (auth.status !== "pending") return;
    const timer = setInterval(() => { void refresh(); }, 2000);
    return () => clearInterval(timer);
  }, [auth.status]);

  useEffect(() => {
    let active = true;
    setModels([]);
    if (!enabled || !provider.chatgpt_account_id || !selected?.connected || !selected.plan_enabled) return;
    setLoadingModels(true);
    apiGet<{ models: { id: string; name: string }[] }>(`/api/config/chatgpt/models?account_id=${encodeURIComponent(provider.chatgpt_account_id)}`)
      .then((result) => { if (active) setModels(result.models); })
      .catch((e) => { if (active) setError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (active) setLoadingModels(false); });
    return () => { active = false; };
  }, [enabled, provider.chatgpt_account_id, selected?.connected, selected?.plan_enabled]);

  async function login(accountId: string) {
    const popup = window.open("about:blank", "_blank");
    if (popup) popup.opener = null;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await apiPost<{ url: string }>("/api/config/chatgpt/start", { account_id: accountId });
      if (popup) popup.location.href = result.url;
      else { await apiPost("/api/config/chatgpt/cancel", {}); throw new Error(zh ? "请允许弹出登录窗口后重试" : "Allow the sign-in popup and try again"); }
      await refresh();
    } catch (e) { popup?.close(); setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  async function loadModels() {
    setLoadingModels(true); setError("");
    try {
      const result = await apiGet<{ models: { id: string; name: string }[] }>(`/api/config/chatgpt/models?account_id=${encodeURIComponent(provider.chatgpt_account_id || "")}`);
      setModels(result.models);
      if (!result.models.length) setNotice(zh ? "此账号暂未返回可用模型，请检查套餐及应用权限。" : "No available models were returned. Check your plan and app permissions.");
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setLoadingModels(false); }
  }

  async function logout() {
    setBusy(true); setError("");
    try {
      const result = await apiPost<{ message: string }>("/api/config/chatgpt/sign-out", { account_id: provider.chatgpt_account_id });
      setNotice(result.message); setModels([]); await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  return <div className="space-y-3 rounded-xl border border-border bg-background/60 p-4 md:col-span-2">
    <label className="block space-y-2 text-sm">
      <span className="font-medium">{zh ? "登录方式" : "Authentication"}</span>
      <select className={selectClass} value={enabled ? "chatgpt" : "api_key"} onChange={(event) => {
        const mode = event.target.value as "api_key" | "chatgpt";
        onChange(mode === "chatgpt" ? { auth_mode: mode, base_url: "https://api.openai.com", api_mode: "responses", supports_streaming: true } : { auth_mode: mode });
      }}>
        <option value="api_key">API Key</option>
        <option value="chatgpt">{zh ? "ChatGPT 网页授权" : "Sign in with ChatGPT"}</option>
      </select>
    </label>
    {enabled && <>
      <p className="text-xs leading-5 text-muted">{zh ? "在 OpenAI 页面登录并授权使用 ChatGPT 套餐，无需填写 API Key。可用模型与额度以你的账号为准；不会读取 ChatGPT 历史对话。" : "Sign in on OpenAI and authorize eligible ChatGPT plan usage. No API key is needed. Models and usage depend on your account; chat history is not accessed."}</p>
      <label className="block space-y-2 text-sm">
        <span>{zh ? "ChatGPT 账号" : "ChatGPT account"}</span>
        <select className={selectClass} value={provider.chatgpt_account_id || ""} onChange={(e) => onChange({ chatgpt_account_id: e.target.value })}>
          <option value="">{zh ? "登录后请选择账号" : "Select an account after sign-in"}</option>
          {auth.accounts.map((a) => <option key={a.id} value={a.id}>{a.label}{a.connected ? "" : zh ? "（已退出）" : " (signed out)"}</option>)}
        </select>
      </label>
      {selected && <p className="text-xs text-muted">{selected.connected ? (selected.plan_enabled ? (zh ? "已连接 · 套餐权限已授权" : "Connected · Plan usage authorized") : (zh ? "已登录 · 请重新授权套餐使用权限" : "Signed in · Authorize plan usage")) : (zh ? "请重新登录此账号" : "Sign in to this account again")}</p>}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant={selected?.connected ? "outline" : "primary"} isDisabled={busy || auth.status === "pending"} onPress={() => { void login(provider.chatgpt_account_id || ""); }}>{selected?.connected ? (zh ? "重新授权" : "Reauthorize") : "Continue with ChatGPT"}</Button>
        {selected && <Button size="sm" variant="outline" isDisabled={busy || auth.status === "pending"} onPress={() => { void login(""); }}>{zh ? "添加其他账号" : "Add another account"}</Button>}
        {selected?.connected && <Button size="sm" variant="ghost" isDisabled={busy} onPress={() => { void logout(); }}>{zh ? "退出账号" : "Sign out"}</Button>}
        {auth.status === "pending" && <Button size="sm" variant="ghost" onPress={async () => { await apiPost("/api/config/chatgpt/cancel", {}); await refresh(); }}>{zh ? "取消等待" : "Cancel sign-in"}</Button>}
      </div>
      {auth.status === "pending" && <p role="status" className="text-sm text-muted">{zh ? "等待浏览器授权，完成后请在上方选择账号。" : "Waiting for browser authorization. Select the account above when complete."}</p>}
      {auth.status !== "pending" && auth.message && !selected?.connected && <p role="status" className="text-sm text-muted">{auth.message}</p>}
      {selected?.connected && selected.plan_enabled && <div className="space-y-2 border-t border-border pt-3">
        <Button size="sm" variant="outline" isDisabled={busy || loadingModels} onPress={() => { void loadModels(); }}>{loadingModels ? (zh ? "正在读取模型…" : "Loading models…") : (zh ? "刷新可用模型" : "Refresh models")}</Button>
        <label className="block space-y-2 text-sm"><span>{zh ? "选择模型" : "Choose model"}</span><select aria-label={zh ? "选择模型" : "Choose model"} className={selectClass} value={provider.default_model} onChange={(e) => {
          const model = e.target.value;
          const profile = getChatGPTReasoningProfile(model);
          onChange({ default_model: model, ...(provider.reasoning_effort && !profile.efforts.includes(provider.reasoning_effort) ? { reasoning_effort: null } : {}) });
        }}><option value="">{zh ? "请选择" : "Select a model"}</option>{modelOptions.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}</select></label>
        <label className="block space-y-2 pt-2 text-sm"><span className="font-medium">{zh ? "模型推理强度" : "Reasoning effort"}</span>
          <select aria-label={zh ? "模型推理强度" : "Reasoning effort"} className={selectClass} value={provider.reasoning_effort ?? "default"} onChange={(e) => onChange({ reasoning_effort: e.target.value === "default" ? null : e.target.value as ProviderConfig["reasoning_effort"] })}>
            <option value="default">{zh ? "模型默认" : "Model default"}{reasoning.defaultEffort ? `（${effortNames[reasoning.defaultEffort]} / ${reasoning.defaultEffort}）` : ""}</option>
            {[...new Set([...reasoning.efforts, ...(provider.reasoning_effort ? [provider.reasoning_effort] : [])])].map((effort) => <option key={effort} value={effort}>{effortNames[effort]} / {effort}</option>)}
          </select>
        </label>
        <p className="text-xs leading-5 text-muted">{zh ? "强度越高，通常思考更充分，也会增加等待时间与用量。这里设置 Provider 默认值；流程节点或单次请求显式设置的推理参数优先。" : "Higher effort usually means more reasoning, latency, and usage. This sets the provider default; explicit workflow or request settings take priority."}</p>
        {!reasoning.defaultEffort && <p className="text-xs text-muted">{zh ? "此模型的默认档位和支持范围以服务端为准；不确定时保留“模型默认”。" : "Supported efforts and defaults depend on the model; keep Model default when unsure."}</p>}
        <p className="text-xs text-muted">{zh ? "账号授权已保存在本项目，无需重复登录。保存设置后，登录方式、账号、模型与推理强度将在刷新或重启后恢复。" : "Authorization is stored for this project. Save settings to retain the authentication method, account, model, and effort across reloads and restarts."}</p>
      </div>}
      <p className="text-xs text-muted">{zh ? "选好账号和模型后，启用 Provider 并点击设置页右上角保存。" : "Select an account and model, enable this provider, then save settings."}</p>
    </>}
    {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
    {notice && <p role="status" className="text-sm text-muted">{notice}</p>}
  </div>;
}
