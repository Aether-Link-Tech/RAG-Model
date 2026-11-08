// Solves the backend's proof-of-work captcha (FR-001 / FR-002).
// The server sends SHA-256(salt + n); we find n by brute force with Web Crypto.
// That takes about a second of CPU, which is cheap for a person and expensive
// for a bot sending thousands of requests. No third-party service is involved.
import api from './api';
import { USE_MOCK } from './config';
import type { CaptchaChallenge } from '../types';

const encoder = new TextEncoder();

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', encoder.encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
}

export async function fetchChallenge(): Promise<CaptchaChallenge> {
  const { data } = await api.get<CaptchaChallenge>('/auth/captcha');
  return data;
}

/** Find the secret number; yields to the UI every batch so the spinner keeps moving. */
export async function solveChallenge(challenge: CaptchaChallenge, signal?: AbortSignal): Promise<string> {
  const BATCH = 500;
  for (let start = 0; start <= challenge.maxnumber; start += BATCH) {
    if (signal?.aborted) throw new DOMException('Captcha cancelled', 'AbortError');
    const end = Math.min(start + BATCH - 1, challenge.maxnumber);
    for (let number = start; number <= end; number += 1) {
      if ((await sha256Hex(`${challenge.salt}${number}`)) === challenge.challenge) {
        const payload = {
          algorithm: challenge.algorithm,
          challenge: challenge.challenge,
          number,
          salt: challenge.salt,
          signature: challenge.signature,
        };
        return btoa(JSON.stringify(payload));
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  throw new Error('Captcha could not be solved. Please try again.');
}

/** Fetch + solve; returns the payload the backend expects in the `captcha` field. */
export async function verifyHuman(signal?: AbortSignal): Promise<string> {
  if (USE_MOCK) {
    await new Promise((resolve) => setTimeout(resolve, 600));
    return 'mock-captcha';
  }
  return solveChallenge(await fetchChallenge(), signal);
}
