import { CameraTools } from "./CameraTools";
import { t } from "./i18n";
import { shareFrames } from "./cameraFrames";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { IAgoraRTCClient } from "agora-rtc-sdk-ng";
import { socketUrl, type Capability } from "./api";

export function DirectCamera({
  capability,
  overlay,
}: {
  capability: Capability;
  overlay?: ReactNode;
}) {
  const panel = useRef<HTMLElement>(null);
  const container = useRef<HTMLDivElement>(null);
  const [attempt, setAttempt] = useState(1);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState("Connecting to video provider");
  useEffect(() => {
    if (!attempt || !capability.supported) return;
    setLoading(true);
    const target = container.current;
    const stopFrames = shareFrames(target);
    let client: IAgoraRTCClient | undefined;
    let disposed = false;
    let joined = false;
    let published = false;
    let lastVideoTime = -1;
    let identity = "";
    let phase = "subscription";
    let recoveries = 0;
    let recovery: ReturnType<typeof setTimeout> | undefined;
    let handling = Promise.resolve();
    const ws = new WebSocket(socketUrl("/camera"));
    const timeout = setTimeout(() => {
      setStatus(
        "No video received. Check camera availability and Internet access, then reconnect.",
      );
      setLoading(false);
      ws.close();
    }, 45000);
    const heartbeat = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ type: "heartbeat" }));
    }, 15000);
    const fail = (error?: unknown) => {
      const value = error as { code?: unknown } | undefined;
      const code =
        typeof value?.code === "string" && /^[A-Z0-9_]{1,60}$/.test(value.code)
          ? value.code
          : "VIDEO_ERROR";
      if (!disposed) {
        setLoading(false);
        setStatus(
          `${t("Camera connection failed")}: ${phase} (${code}). ${t("Check camera availability and network connection.")}`,
        );
      }
      ws.close();
    };
    const decoded = setInterval(() => {
      const video = container.current?.querySelector("video");
      if (
        !disposed &&
        published &&
        video &&
        video.readyState >= 2 &&
        video.videoWidth > 0 &&
        !video.paused &&
        video.currentTime > lastVideoTime
      ) {
        lastVideoTime = video.currentTime;
        clearTimeout(timeout);
        setLoading(false);
        setStatus("Live · Mammotion / Agora");
      }
    }, 500);
    ws.onmessage = (event) => {
      handling = handling
        .then(async () => {
          const data = JSON.parse(event.data);
          if (disposed) return;
          if (data.type === "error") {
            setLoading(false);
            setStatus(data.message);
            ws.close();
            return;
          }
          if (data.type !== "agora") return;
          const nextIdentity = JSON.stringify([
            data.appid,
            data.channel,
            data.uid,
          ]);
          if (joined && client && identity === nextIdentity) {
            await client.renewToken(data.token);
            return;
          }
          if (client) {
            client.removeAllListeners();
            await client.leave();
            joined = false;
            published = false;
            lastVideoTime = -1;
          }
          identity = nextIdentity;
          phase = "sdk";
          const { default: AgoraRTC } = await import("agora-rtc-sdk-ng");
          if (disposed) return;
          AgoraRTC.setLogLevel(4);
          AgoraRTC.disableLogUpload();
          client = AgoraRTC.createClient({ mode: "live", codec: "h264" });
          if (data.encryption) {
            phase = "encryption";
            const salt = Uint8Array.from(atob(data.encryption.salt), (c) =>
              c.charCodeAt(0),
            );
            client.setEncryptionConfig(
              "aes-256-gcm2",
              data.encryption.key,
              salt,
            );
          }
          await client.setClientRole("audience");
          if (disposed) return;
          client.on("user-published", async (user, media) => {
            if (!client || disposed || media !== "video") return;
            try {
              phase = "subscribe";
              await client.subscribe(user, media);
              if (container.current && !disposed) {
                published = true;
                user.videoTrack?.play(container.current);
                setStatus(t("Receiving video frames"));
              }
            } catch (error) {
              fail(error);
            }
          });
          client.on("user-unpublished", (_user, media) => {
            if (media !== "video" || disposed) return;
            published = false;
            setLoading(true);
            setStatus(t("Mower stopped publishing video; reconnect to retry"));
            if (recoveries++ === 0)
              recovery = setTimeout(() => {
                if (!disposed && ws.readyState === WebSocket.OPEN)
                  ws.send(JSON.stringify({ type: "renew" }));
              }, 2000);
          });
          client.on("token-privilege-will-expire", () => {
            if (ws.readyState === WebSocket.OPEN)
              ws.send(JSON.stringify({ type: "renew" }));
          });
          setStatus("Joining Mammotion camera…");
          phase = "join";
          await client.join(data.appid, data.channel, data.token, data.uid);
          joined = true;
          if (disposed) await client.leave();
        })
        .catch(fail);
    };
    ws.onerror = fail;
    ws.onclose = () => {
      stopFrames();
      clearTimeout(timeout);
      clearInterval(heartbeat);
      clearInterval(decoded);
      clearTimeout(recovery);
      client?.removeAllListeners();
      void client?.leave().catch(() => {});
      if (!disposed)
        setStatus((old) =>
          old.startsWith("Live") || old.startsWith("Joining")
            ? "Camera disconnected. Reconnect to retry."
            : old,
        );
      disposed = true;
    };
    return () => {
      stopFrames();
      disposed = true;
      clearTimeout(timeout);
      clearInterval(heartbeat);
      clearInterval(decoded);
      clearTimeout(recovery);
      client?.removeAllListeners();
      void client?.leave().catch(() => {});
      ws.close();
      target?.replaceChildren();
    };
  }, [attempt, capability.supported]);
  return (
    <section ref={panel} className="panel camera-panel">
      <div className="section-title">
        <div>
          <span className="eyebrow">{t("FROM THE MOWER")}</span>
          <h2>{t("Live camera")}</h2>
        </div>
        <button
          disabled={!capability.supported}
          onClick={() => setAttempt(attempt + 1)}
        >
          {attempt ? t("Reconnect") : t("Connect camera")}
        </button>
      </div>
      <div className="video-frame">
        <div className="direct-video" ref={container} />
        {overlay}
        {attempt > 0 && loading && capability.supported && (
          <div className="video-loading" aria-live="polite">
            <span className="loading-spinner" />
            <strong>{t("Starting live video…")}</strong>
            <small>{t(status)}</small>
          </div>
        )}
        {(!attempt || !capability.supported) && (
          <div className="video-empty">
            <h3>{t("Mammotion camera")}</h3>
            <p>{capability.reason}</p>
          </div>
        )}
      </div>
      <p role="status">{t(status)}</p>
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
