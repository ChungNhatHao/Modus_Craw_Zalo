const $ = (selector) => document.querySelector(selector);

const elements = {
  authPill: $("#auth-pill"),
  authMessage: $("#auth-message"),
  authStart: $("#auth-start"),
  authComplete: $("#auth-complete"),
  form: $("#crawl-form"),
  groups: $("#groups"),
  groupCount: $("#group-count"),
  date: $("#crawl-date"),
  timezone: $("#timezone-note"),
  crawlStart: $("#crawl-start"),
  crawlPill: $("#crawl-pill"),
  crawlMessage: $("#crawl-message"),
  progressBar: $("#progress-bar"),
  progressCount: $("#progress-count"),
  currentGroup: $("#current-group"),
  results: $("#results"),
  aiCard: $("#ai-results-card"),
  aiSubtitle: $("#ai-results-subtitle"),
  aiPill: $("#ai-results-pill"),
  aiSummary: $("#ai-summary"),
  aiToolbar: $("#ai-toolbar"),
  aiResults: $("#ai-results"),
  aiVisibleCount: $("#ai-visible-count"),
  summaryMessages: $("#summary-messages"),
  summaryOrders: $("#summary-orders"),
  summaryImages: $("#summary-images"),
  summaryClassified: $("#summary-classified"),
  toast: $("#toast"),
};

let toastTimer;
let loadedRunId = null;
let loadingRunId = null;
let currentRun = null;
let currentFilter = "orders";

function groupsFromInput() {
  return [...new Set(elements.groups.value.split("\n").map((value) => value.trim()).filter(Boolean))];
}

function updateGroupCount() {
  const count = groupsFromInput().length;
  elements.groupCount.textContent = `${count} nhóm`;
}

function showToast(message, error = false) {
  clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.className = `toast show${error ? " error" : ""}`;
  toastTimer = setTimeout(() => { elements.toast.className = "toast"; }, 4200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "Yêu cầu thất bại.");
  return value;
}

function setPill(element, label, style) {
  element.className = `status-pill ${style}`;
  element.querySelector("span").textContent = label;
}

function renderAuth(auth) {
  const state = auth.state || "idle";
  const mapping = {
    idle: ["Chưa xác thực", "idle"],
    starting: ["Đang mở Zalo", "running"],
    waiting_login: ["Chờ đăng nhập", "waiting"],
    ready: ["Chờ đồng bộ", "warning"],
    closing: ["Đang lưu phiên", "running"],
    completed: ["Đã sẵn sàng", "success"],
    error: ["Có lỗi", "error"],
    cancelled: ["Đã hủy", "warning"],
  };
  const [label, style] = mapping[state] || [state, "idle"];
  setPill(elements.authPill, label, style);
  elements.authMessage.textContent = auth.message || "";
  const active = ["starting", "waiting_login", "ready", "closing"].includes(state);
  elements.authStart.disabled = active;
  elements.authComplete.disabled = state !== "ready";
}

function renderCrawl(crawl) {
  const state = crawl.state || "idle";
  const mapping = {
    idle: ["Sẵn sàng", "idle"],
    queued: ["Trong hàng đợi", "waiting"],
    running: ["Đang crawl", "running"],
    completed: ["Hoàn tất", "success"],
    completed_with_errors: ["Có nhóm bị lỗi", "warning"],
    error: ["Có lỗi", "error"],
  };
  const [label, style] = mapping[state] || [state, "idle"];
  setPill(elements.crawlPill, label, style);
  elements.crawlMessage.textContent = crawl.message || "";

  const total = Number(crawl.total || 0);
  const completed = Number(crawl.completed || 0);
  const percent = total ? Math.min(100, (completed / total) * 100) : 0;
  elements.progressBar.style.width = `${percent}%`;
  elements.progressCount.textContent = `${completed} / ${total} nhóm`;
  elements.currentGroup.textContent = crawl.current_group || (state === "completed" ? "Đã hoàn tất" : "Chưa bắt đầu");
  elements.crawlStart.disabled = ["queued", "running"].includes(state);
  renderResults(crawl.results || [], state);
}

function renderResults(results, crawlState) {
  if (!results.length) {
    elements.results.className = "results empty-results";
    elements.results.textContent = "Kết quả của từng nhóm sẽ xuất hiện tại đây.";
    return;
  }
  elements.results.className = "results";
  elements.results.replaceChildren(...results.map((result) => {
    const item = document.createElement("div");
    item.className = `result-item ${result.ok ? "ok" : "fail"}`;

    const icon = document.createElement("div");
    icon.className = "result-icon";
    icon.textContent = result.ok ? "✓" : "!";

    const content = document.createElement("div");
    const name = document.createElement("div");
    name.className = "result-name";
    name.textContent = result.group;
    const message = document.createElement("div");
    message.className = "result-message";
    message.textContent = result.message || "";
    content.append(name, message);

    const actions = document.createElement("div");
    actions.className = "result-actions";
    const path = document.createElement("div");
    path.className = "result-path";
    path.title = result.output_dir || "";
    path.textContent = result.output_dir || "Không có đầu ra";
    actions.append(path);
    const drive = result.google_drive || {};
    const driveLinks = [
      [drive.sheet, "Mở Google Sheet"],
      [drive.image_folder, "Mở thư mục ảnh"],
      [drive.branch_config, "Mở cấu hình chi nhánh"],
    ];
    driveLinks.forEach(([resource, label]) => {
      if (!resource || !resource.url) return;
      const link = document.createElement("a");
      link.className = "result-drive-link";
      link.href = resource.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = label;
      actions.append(link);
    });
    if (result.ok && result.run_id) {
      const view = document.createElement("button");
      view.type = "button";
      view.className = "result-view-button";
      view.textContent = "Xem kết quả AI";
      view.addEventListener("click", async () => {
        try {
          await loadRunResult(result.run_id);
          elements.aiCard.scrollIntoView({ behavior: "smooth", block: "start" });
        } catch (error) {
          showToast(error.message, true);
        }
      });
      actions.append(view);
    }
    item.append(icon, content, actions);
    return item;
  }));

  const latest = [...results].reverse().find((result) => result.ok && result.run_id);
  if (
    latest &&
    ["completed", "completed_with_errors"].includes(crawlState) &&
    latest.run_id !== loadedRunId &&
    latest.run_id !== loadingRunId
  ) {
    loadRunResult(latest.run_id).catch((error) => showToast(error.message, true));
  }
}

function setAiPill(label, style) {
  setPill(elements.aiPill, label, style);
}

function clearAiResults() {
  loadedRunId = null;
  loadingRunId = null;
  currentRun = null;
  elements.aiSummary.hidden = true;
  elements.aiToolbar.hidden = true;
  elements.aiSubtitle.textContent = "Kết quả đơn hàng và hình ảnh sẽ tự xuất hiện sau khi crawl xong.";
  elements.aiResults.className = "ai-message-list empty-results";
  elements.aiResults.textContent = "Đang chờ lượt crawl mới hoàn tất.";
  setAiPill("Đang chờ", "waiting");
}

async function loadRunResult(runId) {
  if (!runId) return;
  if (runId === loadedRunId && currentRun) {
    renderAiMessages();
    return;
  }
  if (runId === loadingRunId) return;
  loadingRunId = runId;
  setAiPill("Đang tải kết quả", "running");
  elements.aiResults.className = "ai-message-list empty-results";
  elements.aiResults.textContent = "Đang đọc kết quả AI và hình ảnh...";
  try {
    currentRun = await api(`/api/run-result?id=${encodeURIComponent(runId)}`);
    loadedRunId = runId;
    const summary = currentRun.summary || {};
    elements.aiSubtitle.textContent = `${currentRun.group_name} · ${formatDate(currentRun.target_date)}`;
    elements.summaryMessages.textContent = summary.clean_messages || 0;
    elements.summaryOrders.textContent = summary.orders || 0;
    elements.summaryImages.textContent = summary.message_images || 0;
    elements.summaryClassified.textContent = summary.classified_messages || 0;
    elements.aiSummary.hidden = false;
    elements.aiToolbar.hidden = false;
    setAiPill("Đã có kết quả", "success");
    renderAiMessages();
  } catch (error) {
    setAiPill("Không tải được", "error");
    elements.aiResults.className = "ai-message-list empty-results";
    elements.aiResults.textContent = error.message;
    throw error;
  } finally {
    loadingRunId = null;
  }
}

function formatDate(value) {
  if (!value) return "Không rõ ngày";
  const parts = String(value).split("-");
  return parts.length === 3 ? `${parts[2]}/${parts[1]}/${parts[0]}` : value;
}

function decisionMatchesFilter(message) {
  const isOrder = Boolean(message.decision && message.decision.is_order);
  if (currentFilter === "orders") return isOrder;
  if (currentFilter === "non-orders") return !isOrder;
  return true;
}

function confidencePercent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.round(number * 100) : null;
}

function confidenceTone(percent) {
  if (percent === null || percent < 70) return "low";
  if (percent < 90) return "medium";
  return "high";
}

function renderAiMessages() {
  if (!currentRun) return;
  const messages = (currentRun.messages || []).filter(decisionMatchesFilter);
  elements.aiVisibleCount.textContent = `${messages.length} tin đang hiển thị`;
  document.querySelectorAll(".filter-chip").forEach((button) => {
    button.classList.toggle("active", button.dataset.filter === currentFilter);
  });
  if (!messages.length) {
    elements.aiResults.className = "ai-message-list empty-results";
    elements.aiResults.textContent = "Không có tin nhắn phù hợp với bộ lọc này.";
    return;
  }
  elements.aiResults.className = "ai-message-list";
  elements.aiResults.replaceChildren(...messages.map(renderAiMessage));
}

function renderAiMessage(message) {
  const decision = message.decision || {};
  const isOrder = Boolean(decision.is_order);
  const card = document.createElement("article");
  card.className = `ai-message-card ${isOrder ? "is-order" : "not-order"}`;

  const header = document.createElement("div");
  header.className = "ai-message-header";
  const identity = document.createElement("div");
  const sender = document.createElement("strong");
  sender.textContent = message.sender || (message.direction === "outgoing" ? "Bạn" : "Không rõ người gửi");
  const meta = document.createElement("span");
  meta.textContent = [message.time, message.message_type === "image" ? "Tin có ảnh" : "Tin văn bản"].filter(Boolean).join(" · ");
  identity.append(sender, meta);
  const badges = document.createElement("div");
  badges.className = "decision-badges";
  const badge = document.createElement("span");
  badge.className = `decision-badge ${isOrder ? "order" : "not-order"}`;
  badge.textContent = isOrder ? "Đơn hàng" : "Không phải đơn";
  badges.append(badge);
  if (decision.needs_review) {
    const review = document.createElement("span");
    review.className = "decision-badge review";
    review.textContent = "Cần kiểm tra";
    badges.append(review);
  }
  header.append(identity, badges);

  const content = document.createElement("div");
  content.className = "message-copy";
  content.textContent = message.content || "[Không có nội dung chữ]";
  card.append(header, content);

  const images = (message.media || []).filter((media) => String(media.mime_type || "").startsWith("image/"));
  if (images.length) {
    const gallery = document.createElement("div");
    gallery.className = "media-gallery";
    images.forEach((media) => {
      const figure = document.createElement("figure");
      const link = document.createElement("a");
      link.href = media.url;
      link.target = "_blank";
      link.rel = "noopener";
      const image = document.createElement("img");
      image.src = media.url;
      image.loading = "lazy";
      image.alt = media.role === "message_image" ? "Ảnh đính kèm của tin nhắn" : "Ảnh xem trước đường link";
      const caption = document.createElement("figcaption");
      caption.textContent = media.role === "message_image" ? "Ảnh tin nhắn" : "Thumbnail đường link";
      link.append(image);
      figure.append(link, caption);
      gallery.append(figure);
    });
    card.append(gallery);
  }

  const evaluation = document.createElement("div");
  evaluation.className = "ai-evaluation";
  const confidenceRow = document.createElement("div");
  confidenceRow.className = "confidence-row";
  const orderPercent = confidencePercent(decision.confidence);
  const orderConfidence = document.createElement("span");
  orderConfidence.className = `confidence-chip ${confidenceTone(orderPercent)}`;
  orderConfidence.textContent = `Nhận diện đơn: ${orderPercent === null ? "Chưa có" : `${orderPercent}%`}`;
  confidenceRow.append(orderConfidence);
  if (isOrder) {
    const dataPercent = confidencePercent(decision.data_confidence);
    const dataConfidence = document.createElement("span");
    dataConfidence.className = `confidence-chip ${confidenceTone(dataPercent)}`;
    dataConfidence.textContent = `Thông tin đơn: ${dataPercent === null ? "Chưa có" : `${dataPercent}%`}`;
    confidenceRow.append(dataConfidence);
  }
  evaluation.append(confidenceRow);
  const reason = document.createElement("p");
  reason.textContent = decision.reason || "AI chưa trả lý do";
  evaluation.append(reason);

  const products = Array.isArray(decision.products) ? decision.products : [];
  const quantities = Array.isArray(decision.quantities) ? decision.quantities : [];
  if (products.length) {
    const list = document.createElement("ul");
    list.className = "product-list";
    products.forEach((product, index) => {
      const item = document.createElement("li");
      item.textContent = quantities[index] ? `${product} — ${quantities[index]}` : product;
      list.append(item);
    });
    evaluation.append(list);
  }

  const details = [
    ["Chi nhánh", decision.branch_name],
    ["Khách hàng", decision.customer_name],
    ["Điện thoại", decision.phone],
    ["Địa chỉ", decision.address],
    ["Ghi chú", decision.notes],
  ].filter(([, value]) => value);
  if (details.length) {
    const grid = document.createElement("dl");
    grid.className = "order-detail-grid";
    details.forEach(([label, value]) => {
      const term = document.createElement("dt");
      term.textContent = label;
      const description = document.createElement("dd");
      description.textContent = value;
      grid.append(term, description);
    });
    evaluation.append(grid);
  }
  card.append(evaluation);
  return card;
}

async function refreshStatus() {
  try {
    const value = await api("/api/status");
    renderAuth(value.auth);
    renderCrawl(value.crawl);
  } catch (error) {
    showToast(error.message, true);
  }
}

elements.authStart.addEventListener("click", async () => {
  try {
    renderAuth({ state: "starting", message: "Đang mở Zalo Web..." });
    await api("/api/auth/start", { method: "POST", body: "{}" });
    showToast("Đã yêu cầu mở cửa sổ Zalo.");
    await refreshStatus();
  } catch (error) {
    showToast(error.message, true);
    await refreshStatus();
  }
});

elements.authComplete.addEventListener("click", async () => {
  try {
    await api("/api/auth/complete", { method: "POST", body: "{}" });
    showToast("Đang lưu phiên đăng nhập và đóng Zalo.");
    await refreshStatus();
  } catch (error) {
    showToast(error.message, true);
  }
});

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const groups = groupsFromInput();
  if (!groups.length) {
    showToast("Hãy nhập ít nhất một tên nhóm.", true);
    elements.groups.focus();
    return;
  }
  try {
    clearAiResults();
    elements.crawlStart.disabled = true;
    await api("/api/crawl/start", {
      method: "POST",
      body: JSON.stringify({ groups, date: elements.date.value }),
    });
    showToast(`Đã bắt đầu crawl ${groups.length} nhóm.`);
    await refreshStatus();
  } catch (error) {
    elements.crawlStart.disabled = false;
    showToast(error.message, true);
  }
});

elements.groups.addEventListener("input", updateGroupCount);

document.querySelectorAll(".filter-chip").forEach((button) => {
  button.addEventListener("click", () => {
    currentFilter = button.dataset.filter || "orders";
    renderAiMessages();
  });
});

async function initialise() {
  try {
    const bootstrap = await api("/api/bootstrap");
    elements.date.value = bootstrap.today;
    elements.timezone.textContent = `Mặc định hôm nay · Múi giờ ${bootstrap.timezone}`;
    if (!elements.groups.value && bootstrap.default_groups.length) {
      elements.groups.value = bootstrap.default_groups.join("\n");
    }
    updateGroupCount();
    await refreshStatus();
    setInterval(refreshStatus, 1500);
  } catch (error) {
    showToast(error.message, true);
  }
}

initialise();
