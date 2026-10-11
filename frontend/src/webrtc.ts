export async function completeOffer(peer: RTCPeerConnection): Promise<string> {
  const offer = await peer.createOffer();
  await peer.setLocalDescription(offer);
  // Mammotion's HA bridge constructs the Agora join from the initial SDP.
  // Include gathered candidates instead of racing join against trickle ICE.
  if (peer.iceGatheringState !== "complete") {
    await new Promise<void>((resolve) => {
      const done = () => {
        clearTimeout(timer);
        peer.removeEventListener("icegatheringstatechange", changed);
        resolve();
      };
      const changed = () => {
        if (peer.iceGatheringState === "complete") done();
      };
      const timer = setTimeout(done, 8000);
      peer.addEventListener("icegatheringstatechange", changed);
      changed();
    });
  }
  if (peer.signalingState === "closed" || !peer.localDescription?.sdp)
    throw new Error("Camera connection was closed before the offer was ready");
  return peer.localDescription.sdp;
}
