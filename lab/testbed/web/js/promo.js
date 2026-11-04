// Weekly promo strip at the top of the home page. The text lives in data/promo.json so marketing can change it.
export async function showPromo() {
  const res = await fetch("data/promo.json");
  if (!res.ok) return;
  const promo = await res.json();

  const banner = document.createElement("a");
  banner.className = "promo";
  banner.href = promo.href;
  banner.textContent = promo.text;

  const main = document.querySelector("main");
  main.prepend(banner);
}
