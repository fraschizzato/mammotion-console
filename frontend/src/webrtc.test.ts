import { expect, it, vi } from "vitest";
import { completeOffer } from "./webrtc";

it("sends gathered local SDP instead of the initial candidate-free offer", async () => {
  const peer = {
    createOffer: vi.fn().mockResolvedValue({ type: "offer", sdp: "initial" }),
    setLocalDescription: vi.fn().mockResolvedValue(undefined),
    iceGatheringState: "complete",
    signalingState: "have-local-offer",
    localDescription: { sdp: "offer-with-candidates" },
  };
  expect(await completeOffer(peer as unknown as RTCPeerConnection)).toBe(
    "offer-with-candidates",
  );
});

it("does not send an offer after the viewer closes the connection", async () => {
  const peer = {
    createOffer: vi.fn().mockResolvedValue({}),
    setLocalDescription: vi.fn(),
    iceGatheringState: "complete",
    signalingState: "closed",
  };
  await expect(
    completeOffer(peer as unknown as RTCPeerConnection),
  ).rejects.toThrow("closed");
});
