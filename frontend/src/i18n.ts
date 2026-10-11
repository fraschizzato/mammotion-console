import { useSyncExternalStore } from "react";
import italian from "./it.json";

export type Language = "en" | "it";
const listeners = new Set<() => void>();
let language: Language = "en";
try {
  language = localStorage.getItem("console-language") === "it" ? "it" : "en";
} catch {
  /* Storage can be disabled. */
}
export function setLanguage(value: Language) {
  language = value;
  document.documentElement.lang = value;
  try {
    localStorage.setItem("console-language", value);
  } catch {
    /* Keep the session preference. */
  }
  listeners.forEach((listener) => listener());
}
export function useLanguage() {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    () => language,
  );
}
export function t(text: string): string {
  return language === "it"
    ? (italian as Record<string, string>)[text] || text
    : text;
}
