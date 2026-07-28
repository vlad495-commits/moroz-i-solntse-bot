import http from "k6/http";
import { check } from "k6";

export const options = {
  scenarios: {
    telegram_webhook: {
      executor: "constant-arrival-rate",
      rate: 30,
      timeUnit: "1m",
      duration: "5m",
      preAllocatedVUs: 20,
      maxVUs: 40,
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.05"],
    http_req_duration: ["p(95)<1500"],
  },
};

const BASE_URL = __ENV.PUBLIC_BASE_URL;
const TELEGRAM_WEBHOOK_SECRET = __ENV.TELEGRAM_WEBHOOK_SECRET;

export default function () {
  const payload = JSON.stringify({
    update_id: 100000 + __ITER,
    message: {
      message_id: __ITER,
      date: Math.floor(Date.now() / 1000),
      chat: { id: 700000 + (__VU % 20), type: "private" },
      from: { id: 700000 + (__VU % 20), is_bot: false, first_name: "Load" },
      text: "/start",
    },
  });
  const response = http.post(`${BASE_URL}/telegram/webhook`, payload, {
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": TELEGRAM_WEBHOOK_SECRET,
    },
  });
  check(response, {
    "webhook accepted": (r) => [200, 202].includes(r.status),
  });
}
