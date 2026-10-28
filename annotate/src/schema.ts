/**
 * The shape the model must return. Kept deliberately tight: the extraction runs on a free-tier
 * model, which will invent plausible hockey detail if given room, so the schema is the main thing
 * standing between the commentary and the database.
 *
 * The strongest guard is `quote`: every feature must point at words that actually appear in the
 * commentary supplied. annotate.ts verifies that, and drops features whose quote is not there.
 */

import { z } from "zod";
import { FEATURE_CODES } from "./vocabulary.ts";

export const PROMPT_VERSION = "p4";

export const featureSchema = z.object({
  code: z.enum(FEATURE_CODES).describe("one of the listed feature codes"),
  confidence: z.number().min(0).max(1)
    .describe("0-1. Use >0.8 only when the commentary states it outright, not when it is implied."),
  source: z.enum(["pre", "post"])
    .describe("which window the evidence came from: before the shot, or the replay talk after"),
  quote: z.string().min(3).max(300)
    .describe("the exact words from the commentary that justify this, copied verbatim"),
  corrected_value: z.string().nullable()
    .describe("only for the *_correction codes: what the commentary says it should be; else null"),
});

export const shotAnnotationSchema = z.object({
  event_id: z.number().int().describe("the event_id of the shot this applies to"),
  relevance: z.number().min(0).max(1)
    .describe("0-1: how much of this commentary is about THIS shot. Windows overlap and much of the talk is filler or about another play; 0 is the correct answer more often than not."),
  attribution_confidence: z.number().min(0).max(1)
    .describe("0-1: how sure you are the commentary you used describes this shot and not a neighbouring one"),
  commentary_quality: z.enum(["clear", "noisy", "absent", "unrelated"]),
  // Scoped to PRE on purpose. The post-shot window reveals the result for essentially every
  // shot, so a flag covering both windows is true ~always and measures nothing. What matters is
  // whether the window feeding xg_adjusted_pre was contaminated.
  outcome_leaked: z.boolean()
    .describe("true ONLY if the BEFORE-the-shot commentary already revealed whether it scored or was saved. The after-the-shot window almost always reveals it; that is expected and is NOT a leak."),
  leak_note: z.string().nullable()
    .describe("if outcome_leaked, the words in the BEFORE window that gave it away"),
  summary: z.string().max(200)
    .describe("one plain sentence on what the commentary says about this shot; '' if it says nothing"),
  features: z.array(featureSchema).max(8),
});

export const clusterResponseSchema = z.object({
  shots: z.array(shotAnnotationSchema),
});

export type FeatureOut = z.infer<typeof featureSchema>;
export type ShotAnnotation = z.infer<typeof shotAnnotationSchema>;
export type ClusterResponse = z.infer<typeof clusterResponseSchema>;
