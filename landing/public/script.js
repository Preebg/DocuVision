(() => {
  const reveals = document.querySelectorAll(".reveal");
  if (!reveals.length) return;

  if (!("IntersectionObserver" in window)) {
    reveals.forEach((el) => el.classList.add("is-visible"));
    return;
  }

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      });
    },
    { threshold: 0.18, rootMargin: "0px 0px -8% 0px" }
  );

  reveals.forEach((el, index) => {
    el.style.transitionDelay = `${Math.min(index % 6, 4) * 60}ms`;
    observer.observe(el);
  });
})();
