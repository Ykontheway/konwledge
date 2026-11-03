import raw from "../../demo/snapshot.json";
import { assertSnapshot, SnapshotIndex } from "./lib/snapshot";

/**
 * The committed demo snapshot, bundled at build time. Python's golden-file test
 * and CI keep it identical to a fresh `knowledge demo export`.
 */
export const snapshot = assertSnapshot(raw);
export const index = new SnapshotIndex(snapshot);

export const REPO_URL = "https://github.com/Ykontheway/konwledge";
