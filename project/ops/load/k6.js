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
    headers: { "Content-Type": "application/json" },
  });
  check(response, {
    "webhook accepted or auth-blocked": (r) => [200, 401, 403, 422].includes(r.status),
  });
}
