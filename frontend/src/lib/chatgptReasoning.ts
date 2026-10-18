import type { ReasoningOptions } from "@/types/generation";

type Effort = NonNullable<ReasoningOptions["reasoning_effort"]>;
type ChatGPTModelOption = { id: string; name: string };

/** Keep the saved model selectable while account-specific models are unavailable. */
export function getChatGPTModelOptions(
  configuredModel: string,
  availableModels: ChatGPTModelOption[],
): ChatGPTModelOption[] {
  const uniqueModels = availableModels.filter(
    (model, index, all) => model.id && all.findIndex((candidate) => candidate.id === model.id) === index,
  );
  const current = configuredModel.trim();
  if (!current || uniqueModels.some((model) => model.id === current)) return uniqueModels;
  return [{ id: current, name: current }, ...uniqueModels];
}

/** Model-specific values from OpenAI model documentation; unknown models retain server defaults. */
export function getChatGPTReasoningProfile(model: string): {
  efforts: Effort[]; defaultEffort: Effort | null;
} {
  const name = model.toLowerCase();
  if (/^gpt-5\.6-(terra|sol)(?:-|$)/.test(name)) {
    return { efforts: ["none", "low", "medium", "high", "xhigh", "max"], defaultEffort: "medium" };
  }
  if (/^gpt-6\.1-sol(?:-|$)/.test(name)) {
    return { efforts: ["low", "medium", "high", "xhigh", "max"], defaultEffort: "medium" };
  }
  return { efforts: ["none", "minimal", "low", "medium", "high", "xhigh", "max"], defaultEffort: null };
}
