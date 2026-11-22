/**
 * Local password strength estimate for the change-password form (no network, no dictionary download).
 * Score 0..4 from length, character classes and penalties for the username, repeats, sequences and common words.
 * It is a hint for the user; the server policy (min. 12 characters, not the username, not the current password)
 * is what decides.
 */

export const MIN_PASSWORD_LENGTH = 12;

export const STRENGTH_LABELS = ["Bardzo słabe", "Słabe", "Średnie", "Silne", "Bardzo silne"] as const;

export interface Strength {
  score: 0 | 1 | 2 | 3 | 4;
  label: (typeof STRENGTH_LABELS)[number];
  /** Polish hints for what to improve (empty when the password is strong). */
  hints: string[];
  /** Meets the local mirror of the server policy (length, not the username). */
  meetsPolicy: boolean;
}

const COMMON = ["password", "haslo", "hasło", "qwerty", "admin", "hydra", "nexus", "letmein", "welcome", "zaq1", "abc123", "123456", "111111", "iloveyou", "monkey", "dragon", "polska", "nask"];
const SEQUENCES = ["abcdefghijklmnopqrstuvwxyz", "0123456789", "qwertyuiop", "asdfghjkl", "zxcvbnm"];

function hasSequence(lower: string, run = 4): boolean {
  for (const seq of SEQUENCES) {
    for (let i = 0; i + run <= seq.length; i++) {
      const part = seq.slice(i, i + run);
      if (lower.includes(part) || lower.includes([...part].reverse().join(""))) return true;
    }
  }
  return false;
}

export function estimateStrength(password: string, username = ""): Strength {
  const hints: string[] = [];
  const lower = password.toLowerCase();
  const len = [...password].length;
  const classes = [/[a-ząćęłńóśźż]/, /[A-ZĄĆĘŁŃÓŚŹŻ]/, /\d/, /[^\p{L}\d]/u].filter((r) => r.test(password)).length;
  const user = username.trim().toLowerCase();
  const hasUser = user.length >= 3 && lower.includes(user);

  let points = 0;
  if (len >= 8) points += 1;
  if (len >= MIN_PASSWORD_LENGTH) points += 1;
  if (len >= 16) points += 1;
  if (len >= 20) points += 1;
  points += Math.max(0, classes - 1);

  if (len < MIN_PASSWORD_LENGTH) hints.push(`Użyj co najmniej ${MIN_PASSWORD_LENGTH} znaków.`);
  if (classes < 3 && len < 20) hints.push("Dodaj wielkie litery, cyfry lub znaki specjalne albo wydłuż hasło.");
  if (hasUser) {
    points -= 3;
    hints.push("Hasło nie może zawierać nazwy użytkownika.");
  }
  if (COMMON.some((w) => lower.includes(w))) {
    points -= 2;
    hints.push("Unikaj popularnych słów (np. password, admin, qwerty).");
  }
  if (/(.)\1{2,}/u.test(password)) {
    points -= 1;
    hints.push("Unikaj powtórzeń tego samego znaku.");
  }
  if (hasSequence(lower)) {
    points -= 1;
    hints.push("Unikaj ciągów typu abcd, 1234, qwer.");
  }
  if (len > 0 && new Set(password).size <= 3) points -= 2;

  let score = Math.round(Math.max(0, Math.min(4, (points / 7) * 4))) as Strength["score"];
  if (len < 8) score = 0;
  else if (len < MIN_PASSWORD_LENGTH) score = Math.min(score, 1) as Strength["score"];
  if (hasUser) score = Math.min(score, 1) as Strength["score"];
  return { score, label: STRENGTH_LABELS[score], hints, meetsPolicy: len >= MIN_PASSWORD_LENGTH && !hasUser };
}
