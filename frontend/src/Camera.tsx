import { CameraTools } from "./CameraTools";
import { t } from "./i18n";
import { shareFrames } from "./cameraFrames";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Video, RefreshCw } from "lucide-react";
import { api, socketUrl, type Capability } from "./api";
import { completeOffer } from "./webrtc";
import { DirectCamera } from "./DirectCamera";
import { RelayCamera } from "./RelayCamera";

type CameraProps = { capability: Capability | undefined; overlay?: ReactNode };
export function Camera({ capability, overlay }: CameraProps) {
  const [connection, setConnection] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    const load = () =>
      api("/camera/settings")
        .then((value) => {
          if (active) setConnection(value);
        })
        .catch((err) => {
          if (active) setError(err.message);
        });
    void load();
    const timer = setInterval(load, 3000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);
  const videoCapability =
    connection?.capability ||
    (connection?.mode === "mammotion"
      ? {
          supported: Boolean(connection.configured.mammotion),
          provider: "mammotion",
          reason: t("Connecting to video provider"),
        }
      : capability);
  const direct =
    videoCapability?.provider === "mammotion" ||
    videoCapability?.protocol === "agora";
  return (
    <div className="camera-source-panel">
      <label className="camera-source">
        {t("Video source")}
        <select
          aria-label={t("Video source")}
          value={connection?.mode || "home_assistant"}
          disabled={!connection || busy}
          onChange={async (event) => {
            setBusy(true);
            setError("");
            try {
              setConnection(
                await api("/camera/settings", "PUT", {
                  mode: event.target.value,
                }),
              );
            } catch (err) {
              setError((err as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <option
            value="home_assistant"
            disabled={connection && !connection.configured.home_assistant}
          >
            Home Assistant
          </option>
          <option
            value="mammotion"
            disabled={connection && !connection.configured.mammotion}
          >
            Mammotion cloud
          </option>
        </select>
      </label>
      <p className="muted">
        {t("Video source is independent of mower controls.")}
      </p>
      {error && <p role="alert">{error}</p>}
      {connection?.error && !connection.connected && (
        <p role="alert">{connection.error}</p>
      )}
      {!busy &&
        connection &&
        (direct && connection.relay_supported && videoCapability?.supported ? (
          <RelayCamera key={connection.mode} overlay={overlay} />
        ) : direct ? (
          <DirectCamera
            key={connection?.mode}
            capability={videoCapability}
            overlay={overlay}
          />
        ) : (
          <HaCamera
            key={connection?.mode}
            capability={videoCapability}
            overlay={overlay}
          />
        ))}
    </div>
  );
}

function HaCamera({ capability, overlay }: CameraProps) {
  const panel = useRef<HTMLElement>(null);
  const video = useRef<HTMLVideoElement>(null),
    [status, setStatus] = useState("Negotiating HA WebRTC…"),
    [attempt, setAttempt] = useState(1),
    [loading, setLoading] = useState(true);
  useEffect(() => {
    if (!attempt || !capability?.supported) return;
    setLoading(true);
    const videoElement = video.current;
    const stopFrames = shareFrames(videoElement?.parentElement || null);
    let peer: RTCPeerConnection | undefined,
      session = false,
      disposed = false;
    const pending: RTCIceCandidateInit[] = [],
      remote: RTCIceCandidateInit[] = [];
    const ws = new WebSocket(socketUrl("/camera"));
    const timer = setTimeout(() => {
      setStatus(
        "Connection timed out. Check the camera in Home Assistant and retry.",
      );
      setLoading(false);
      peer?.close();
      ws.close();
    }, 30000);
    ws.onopen = () => {
      setStatus("Negotiating HA WebRTC…");
      ws.send(JSON.stringify({ type: "config" }));
    };
    ws.onmessage = async (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === "error" || msg.success === false)
          throw new Error(
            msg.message ||
              msg.error?.message ||
              "Home Assistant could not start this stream",
          );
        if (msg.type === "result" && msg.id === 1) {
          peer = new RTCPeerConnection(msg.result?.configuration || {});
          const receiver = peer.addTransceiver("video", {
            direction: "recvonly",
          });
          const codecs = RTCRtpReceiver.getCapabilities?.("video")?.codecs;
          if (codecs && receiver.setCodecPreferences)
            receiver.setCodecPreferences(
              [...codecs].sort(
                (a, b) =>
                  Number(b.mimeType.toLowerCase() === "video/h264") -
                  Number(a.mimeType.toLowerCase() === "video/h264"),
              ),
            );
          if (msg.result?.dataChannel)
            peer.createDataChannel(msg.result.dataChannel);
          peer.ontrack = (e) => {
            if (video.current)
              video.current.srcObject =
                e.streams[0] || new MediaStream([e.track]);
            clearTimeout(timer);
            setStatus("Receiving video frames");
          };
          peer.onicecandidate = (e) => {
            if (!e.candidate) return;
            const c = e.candidate.toJSON();
            if (session)
              ws.send(JSON.stringify({ type: "candidate", candidate: c }));
            else pending.push(c);
          };
          peer.onconnectionstatechange = () => {
            if (peer?.connectionState === "failed")
              setStatus("Stream disconnected. Retry to reconnect.");
          };
          const offer = await completeOffer(peer);
          if (!disposed && ws.readyState === WebSocket.OPEN)
            ws.send(JSON.stringify({ type: "offer", offer }));
        }
        const event = msg.event;
        if (event?.type === "session") {
          session = true;
          pending
            .splice(0)
            .forEach((candidate) =>
              ws.send(JSON.stringify({ type: "candidate", candidate })),
            );
        }
        if (event?.type === "answer" && peer) {
          await peer.setRemoteDescription({
            type: "answer",
            sdp: event.answer,
          });
          for (const candidate of remote.splice(0))
            await peer.addIceCandidate(candidate);
        }
        if (event?.type === "candidate") {
          if (peer?.remoteDescription)
            await peer.addIceCandidate(event.candidate);
          else remote.push(event.candidate);
        }
        if (event?.type === "error")
          throw new Error(event.message || "Camera negotiation failed");
      } catch (err) {
        setStatus((err as Error).message);
        setLoading(false);
        clearTimeout(timer);
        peer?.close();
        ws.close();
      }
    };
    ws.onerror = () => {
      setLoading(false);
      setStatus("Camera signaling is unavailable. Retry to reconnect.");
    };
    ws.onclose = () => {
      peer?.close();
      if (videoElement && !disposed) videoElement.srcObject = null;
      stopFrames();
      if (!disposed)
        setStatus((s) =>
          s.startsWith("Live") ? "Stream disconnected. Retry to reconnect." : s,
        );
    };
    return () => {
      stopFrames();
      disposed = true;
      clearTimeout(timer);
      peer?.close();
      ws.close();
      if (videoElement) videoElement.srcObject = null;
    };
  }, [attempt, capability?.supported]);
  return (
    <section ref={panel} className="panel camera-panel">
      <div className="section-title">
        <div>
          <span className="eyebrow">{t("FROM THE MOWER")}</span>
          <h2>{t("Live camera")}</h2>
        </div>
        <Video size={22} />
      </div>
      <div className="video-frame">
        {overlay}
        <video
          ref={video}
          autoPlay
          playsInline
          muted
          onLoadedData={() => {
            setLoading(false);
            setStatus("Live · HA WebRTC / Agora");
          }}
        />
        {attempt > 0 && loading && capability?.supported && (
          <div className="video-loading" aria-live="polite">
            <span className="loading-spinner" />
            <strong>{t("Starting live video…")}</strong>
            <small>{t(status)}</small>
          </div>
        )}
        {(!attempt || !capability?.supported) && (
          <div className="video-empty">
            <Video size={40} />
            <h3>
              {capability?.supported
                ? t("A closer look at your lawn")
                : t("Camera unavailable")}
            </h3>
            <p>
              {capability?.reason ||
                t("Connect Home Assistant to discover camera support.")}
            </p>
          </div>
        )}
      </div>
      <div className="section-title">
        <p role="status">{t(status)}</p>
        <button
          disabled={!capability?.supported}
          onClick={() => setAttempt(attempt + 1)}
        >
          <RefreshCw size={16} />
          {attempt ? t("Reconnect") : t("Connect camera")}
        </button>
      </div>
      <p className="muted">
        {t(
          "The stream uses Home Assistant’s WebRTC integration. Camera credentials are temporary and never saved in the map archive.",
        )}
      </p>
      <CameraTools
        panel={panel}
        active={attempt > 0}
        stop={() => {
          setAttempt(0);
          setLoading(false);
          setStatus("Streaming stopped");
        }}
      />
    </section>
  );
}
