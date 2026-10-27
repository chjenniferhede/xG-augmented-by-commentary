/**
 * Which model to call. Chosen by env so a provider swap is a one-line change and the gold-set
 * evaluation can compare them on the same corpus.
 *
 * Defaults are Google's free tier. Google stopped publishing its free-tier rate limits (the docs
 * now defer to AI Studio), and measured values differ by account, so the limits below are only a
 * starting point -- limiter.ts discovers the real ceiling by handling 429s rather than trusting
 * these numbers.
 */

import { createGoogleGenerativeAI } from "@ai-sdk/google";
import { createGroq } from "@ai-sdk/groq";
import { createOpenRouter } from "@openrouter/ai-sdk-provider";
import type { LanguageModel, ProviderMetadata } from "ai";

export type ProviderName = "google" | "groq" | "openrouter";

export interface ProviderConfig {
  name: ProviderName;
  /** The rotation, in order of preference. */
  modelIds: string[];
  model: (modelId: string) => LanguageModel;
  /** Requests per minute to self-impose. */
  rpm: number;
  /** Token ceiling for one day; the run stops cleanly when it is reached. 0 = no local limit. */
  tokensPerDay: number;
  /** Passed through to generateObject as providerOptions. */
  providerOptions: ProviderMetadata;
}

/**
 * Google's free-tier request quota is counted PER MODEL -- measured, not documented:
 * gemini-3.8-flash returns "limit: 20" for generate_content_free_tier_requests while the other
 * Flash models still answer. So the default is a rotation, not one model: when one runs out of
 * quota the runner moves to the next, which multiplies what a day can get through. Lite models
 * are listed first because they are cheapest to the quota and adequate for extraction.
 *
 * gemini-2.5-* is deliberately absent: Google returns 404 "no longer available to new users".
 */
const DEFAULT_MODELS: Record<ProviderName, string[]> = {
  google: [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.8-flash",
  ],
  groq: ["openai/gpt-oss-120b"],
  openrouter: ["meta-llama/llama-3.3-70b-instruct:free"],
};

// Starting guesses only; see the note above.
const LIMITS: Record<ProviderName, { rpm: number; tokensPerDay: number }> = {
  google: { rpm: 10, tokensPerDay: 0 },
  groq: { rpm: 25, tokensPerDay: 200_000 },
  openrouter: { rpm: 10, tokensPerDay: 0 },
};

const KEY_ENV: Record<ProviderName, string> = {
  google: "GOOGLE_GENERATIVE_AI_API_KEY",
  groq: "GROQ_API_KEY",
  openrouter: "OPENROUTER_API_KEY",
};

export function resolveProvider(overrides: { provider?: string; model?: string } = {}): ProviderConfig {
  const name = (overrides.provider ?? process.env.LLM_PROVIDER ?? "google") as ProviderName;
  if (!(name in DEFAULT_MODELS)) {
    throw new Error(`Unknown LLM_PROVIDER "${name}". Use one of: ${Object.keys(DEFAULT_MODELS).join(", ")}`);
  }
  const apiKey = process.env[KEY_ENV[name]];
  if (!apiKey) {
    throw new Error(
      `${KEY_ENV[name]} is not set in the repo-root .env. ` +
        (name === "google" ? "Get a free key at https://aistudio.google.com/apikey" : ""),
    );
  }
  // LLM_MODEL may be a comma-separated rotation.
  const configured = overrides.model ?? process.env.LLM_MODEL;
  const modelIds = configured
    ? configured.split(",").map((m) => m.trim()).filter(Boolean)
    : DEFAULT_MODELS[name];
  const limits = LIMITS[name];

  let model: (id: string) => LanguageModel;
  let providerOptions: ProviderMetadata = {};
  switch (name) {
    case "google": {
      const google = createGoogleGenerativeAI({ apiKey });
      model = (id) => google(id);
      // structuredOutputs keeps Gemini on native JSON-schema decoding rather than a tool call.
      providerOptions = { google: { structuredOutputs: true } };
      break;
    }
    case "groq": {
      const groq = createGroq({ apiKey });
      model = (id) => groq(id);
      break;
    }
    case "openrouter": {
      const openrouter = createOpenRouter({ apiKey });
      model = (id) => openrouter(id);
      break;
    }
  }

  return {
    name,
    modelIds,
    model,
    rpm: Number(process.env.LLM_RPM ?? limits.rpm),
    tokensPerDay: Number(process.env.LLM_TOKENS_PER_DAY ?? limits.tokensPerDay),
    providerOptions,
  };
}
