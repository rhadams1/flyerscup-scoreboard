const POLL_INTERVAL = 10_000;

document.addEventListener("DOMContentLoaded", () => {
    setCurrentDate();
    loadGames();
    setInterval(loadGames, POLL_INTERVAL);
});

function setCurrentDate() {
    const el = document.getElementById("currentDate");
    if (!el) return;
    el.textContent = new Date().toLocaleDateString("en-US", {
        weekday: "long", month: "long", day: "numeric", year: "numeric",
    });
}

async function loadGames() {
    try {
        const resp = await fetch("/api/today");
        const data = await resp.json();
        renderGames(data.games || []);
        updateTimestamp(data);
    } catch (err) {
        console.error("Error loading games:", err);
    }
}

function updateTimestamp(data) {
    const el = document.getElementById("lastUpdate");
    if (!el) return;
    if (data.last_live_update) {
        el.textContent = "Live update: " + new Date(data.last_live_update).toLocaleTimeString();
    } else if (data.last_schedule_fetch) {
        el.textContent = "Schedule loaded: " + new Date(data.last_schedule_fetch).toLocaleTimeString();
    }
}

function isLive(status) {
    if (!status) return false;
    const s = status.toLowerCase();
    return s.includes("progress") || s === "live" ||
           s.includes("period") || s.includes("intermission") ||
           s.includes("overtime");
}

function sortByTime(a, b) {
    return (a.scheduled_time || "").localeCompare(b.scheduled_time || "");
}

// --- Safe DOM construction (no innerHTML) ---

function el(tag, className, children) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (children) {
        if (!Array.isArray(children)) children = [children];
        children.forEach(c => {
            if (typeof c === "string") node.appendChild(document.createTextNode(c));
            else if (c) node.appendChild(c);
        });
    }
    return node;
}

function renderGames(games) {
    const content = document.getElementById("content");
    if (!content) return;

    if (games.length === 0) {
        content.replaceChildren(el("div", "no-games", "No games scheduled for today"));
        return;
    }

    const todayStr = new Date().toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });

    const live = games.filter(g => isLive(g.status)).sort(sortByTime);
    const upcoming = games.filter(g => !isLive(g.status) && g.status !== "final" && g.status !== "unofficial").sort(sortByTime);
    const allFinal = games.filter(g => g.status === "final" || g.status === "unofficial").sort(sortByTime);

    // Split finals into today vs yesterday
    const todayFinal = allFinal.filter(g => g.date === todayStr);
    const yesterdayFinal = allFinal.filter(g => g.date !== todayStr);

    const sections = [];
    if (live.length > 0) sections.push(renderSection("Live", "live", live));
    if (upcoming.length > 0) sections.push(renderSection("Upcoming", "upcoming", upcoming));
    if (todayFinal.length > 0) sections.push(renderSection("Final", "final", todayFinal));
    if (yesterdayFinal.length > 0) sections.push(renderSection("Yesterday's Results", "final", yesterdayFinal));

    content.replaceChildren(...sections);
}

function renderSection(title, cls, games) {
    const heading = el("h2", "section-title " + cls + "-title", title);
    const grid = el("div", "games-grid");
    games.forEach(g => grid.appendChild(renderCard(g)));

    const section = el("div", "section");
    section.appendChild(heading);
    section.appendChild(grid);
    return section;
}

function renderCard(game) {
    const live = isLive(game.status);
    const isFinal = game.status === "final" || game.status === "unofficial";

    const homeScore = game.scoreboard?.total?.home;
    const visitorScore = game.scoreboard?.total?.visitor;
    // Only show scores for final or live games (upcoming always has 0-0 default)
    const hasScore = (isFinal || live) && homeScore != null && visitorScore != null;

    const homeWon = isFinal && hasScore && homeScore > visitorScore;
    const visitorWon = isFinal && hasScore && visitorScore > homeScore;

    let cardClass = "game-card ";
    if (live) cardClass += "live";
    else if (isFinal) cardClass += "final";
    else cardClass += "upcoming";

    // Status badge
    const badge = el("span", "", "");
    if (live) {
        badge.className = "status-badge live";
        badge.textContent = formatLiveStatus(game.status);
    } else if (isFinal) {
        badge.className = "status-badge final";
        let suffix = "";
        if (game.has_shootout) suffix = " (SO)";
        else if (game.has_overtime) suffix = " (OT)";
        badge.textContent = "Final" + suffix;
    } else {
        badge.className = "status-badge upcoming";
        badge.textContent = game.scheduled_time || "TBD";
    }

    // Header
    const header = el("div", "game-card-header");
    header.appendChild(badge);
    const division = game.home?.division || game.visitor?.division || "";
    if (division) {
        const divBadge = el("span", "division-badge", division);
        header.appendChild(divBadge);
    }

    // Matchup
    const matchup = el("div", "matchup");
    matchup.appendChild(buildTeamRow(game.visitor, hasScore ? visitorScore : null, visitorWon));
    matchup.appendChild(buildTeamRow(game.home, hasScore ? homeScore : null, homeWon));

    // Shots on goal (live games)
    let shotsEl = null;
    if (live && game.scoreboard?.total_shots) {
        const hSog = game.scoreboard.total_shots.home || 0;
        const vSog = game.scoreboard.total_shots.visitor || 0;
        if (hSog || vSog) {
            shotsEl = el("div", "shots-row", "SOG: " + vSog + " - " + hSog);
        }
    }

    // Footer
    const footer = el("div", "game-card-footer");
    const loc = el("span", "location", game.location || "");
    footer.appendChild(loc);
    const link = document.createElement("a");
    link.className = "gs-link";
    link.href = game.gamesheet_url || "#";
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "GameSheet";
    footer.appendChild(link);

    // Assemble card
    const card = el("div", cardClass);
    card.dataset.gameId = game.id;
    card.appendChild(header);
    card.appendChild(matchup);
    if (shotsEl) card.appendChild(shotsEl);
    card.appendChild(footer);
    return card;
}

function buildTeamRow(team, score, isWinner) {
    const row = el("div", "team-row" + (isWinner ? " winner" : ""));

    // Logo
    if (team?.logo_url) {
        const img = document.createElement("img");
        img.src = team.logo_url;
        img.alt = "";
        img.className = "team-logo";
        img.loading = "lazy";
        row.appendChild(img);
    } else {
        row.appendChild(el("div", "team-logo placeholder", initials(team?.name)));
    }

    // Name
    row.appendChild(el("span", "team-name", team?.name || "TBD"));

    // Score
    row.appendChild(el("span", "score", score != null ? String(score) : ""));

    return row;
}

function initials(name) {
    if (!name) return "?";
    return name.split(" ").map(w => w[0]).join("").substring(0, 2).toUpperCase();
}

function formatLiveStatus(status) {
    if (!status) return "Live";
    return status.replace(/\b\w/g, c => c.toUpperCase());
}
