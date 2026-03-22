/* ================================================
   S K Plastic Works — Main JavaScript
   Features: Navbar scroll, mobile menu, counter
   animation, contact form, toast notification
   ================================================ */

'use strict';

/* ---------- Navbar: shrink on scroll + active link ---------- */
const navbar    = document.getElementById('navbar');
const navToggle = document.getElementById('navToggle');
const navLinks  = document.getElementById('navLinks');

window.addEventListener('scroll', () => {
  if (window.scrollY > 60) {
    navbar.classList.add('scrolled');
  } else {
    // Keep .scrolled on inner pages (about.html already has it)
    if (!navbar.classList.contains('always-scrolled')) {
      navbar.classList.remove('scrolled');
    }
  }
}, { passive: true });

/* ---------- Mobile menu toggle ---------- */
if (navToggle) {
  navToggle.addEventListener('click', () => {
    const isOpen = navLinks.classList.toggle('open');
    navToggle.setAttribute('aria-expanded', isOpen);
    navToggle.setAttribute('aria-label', isOpen ? 'Close menu' : 'Open menu');
    // Prevent body scroll when menu is open
    document.body.style.overflow = isOpen ? 'hidden' : '';
  });

  // Close menu when a link is clicked
  navLinks.querySelectorAll('a').forEach(link => {
    link.addEventListener('click', () => {
      navLinks.classList.remove('open');
      navToggle.setAttribute('aria-expanded', 'false');
      navToggle.setAttribute('aria-label', 'Open menu');
      document.body.style.overflow = '';
    });
  });

  // Close menu on outside click
  document.addEventListener('click', (e) => {
    if (!navbar.contains(e.target) && navLinks.classList.contains('open')) {
      navLinks.classList.remove('open');
      navToggle.setAttribute('aria-expanded', 'false');
      document.body.style.overflow = '';
    }
  });
}

/* ---------- Counter animation ---------- */
function animateCounter(el) {
  const target   = parseInt(el.getAttribute('data-target'), 10);
  const duration = 1800; // ms
  const step     = 16;   // ~60fps
  const increment = target / (duration / step);
  let current = 0;

  const timer = setInterval(() => {
    current += increment;
    if (current >= target) {
      el.textContent = target;
      clearInterval(timer);
    } else {
      el.textContent = Math.floor(current);
    }
  }, step);
}

// Trigger counters when stats bar enters viewport
const counterEls = document.querySelectorAll('.stat-number[data-target]');

if (counterEls.length > 0) {
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        animateCounter(entry.target);
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.5 });

  counterEls.forEach(el => observer.observe(el));
}

/* ---------- Toast notification ---------- */
function showToast(message, isError = false) {
  const toast = document.getElementById('toast');
  if (!toast) return;

  toast.textContent = message;
  toast.style.background = isError ? '#c0392b' : 'var(--green-mid)';
  toast.classList.add('show');

  setTimeout(() => toast.classList.remove('show'), 4000);
}

/* ---------- Contact form ---------- */
const contactForm = document.getElementById('contactForm');

if (contactForm) {
  contactForm.addEventListener('submit', (e) => {
    e.preventDefault();

    // Basic validation
    const name    = contactForm.querySelector('#name');
    const phone   = contactForm.querySelector('#phone');
    const product = contactForm.querySelector('#product');

    if (!name.value.trim()) {
      showToast('Please enter your name.', true);
      name.focus();
      return;
    }

    if (!phone.value.trim()) {
      showToast('Please enter your phone number.', true);
      phone.focus();
      return;
    }

    if (!product.value) {
      showToast('Please select a product.', true);
      product.focus();
      return;
    }

    // Build a mailto link as a no-backend fallback
    const subject = encodeURIComponent('Enquiry from Website — ' + (product.options[product.selectedIndex]?.text || ''));
    const body = encodeURIComponent(
      'Name: '    + name.value.trim()                                     + '\n' +
      'Phone: '   + phone.value.trim()                                    + '\n' +
      'Email: '   + (contactForm.querySelector('#email')?.value || '-')   + '\n' +
      'Product: ' + (product.options[product.selectedIndex]?.text || '-') + '\n\n' +
      'Message:\n' + (contactForm.querySelector('#message')?.value || '-')
    );

    // Open mail client with pre-filled details
    window.location.href = `mailto:skplasticworks@gmail.com?subject=${subject}&body=${body}`;

    showToast('✓ Opening your mail app to send the enquiry…');
    contactForm.reset();
  });
}

/* ---------- Smooth scroll for anchor links ---------- */
document.querySelectorAll('a[href^="#"]').forEach(anchor => {
  anchor.addEventListener('click', (e) => {
    const targetId = anchor.getAttribute('href').slice(1);
    const targetEl = document.getElementById(targetId);
    if (targetEl) {
      e.preventDefault();
      const offset = 80; // navbar height
      const top = targetEl.getBoundingClientRect().top + window.scrollY - offset;
      window.scrollTo({ top, behavior: 'smooth' });
    }
  });
});

/* ---------- Scrap seller form ---------- */
const scrapForm = document.getElementById('scrapForm');

if (scrapForm) {
  scrapForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const name     = scrapForm.querySelector('#scrap-name');
    const phone    = scrapForm.querySelector('#scrap-phone');
    const type     = scrapForm.querySelector('#scrap-type');
    const qty      = scrapForm.querySelector('#scrap-qty');
    const location = scrapForm.querySelector('#scrap-location');

    if (!name.value.trim())     { showToast('Please enter your name.', true); name.focus(); return; }
    if (!phone.value.trim())    { showToast('Please enter your phone number.', true); phone.focus(); return; }
    if (!type.value)            { showToast('Please select scrap type.', true); type.focus(); return; }
    if (!qty.value)             { showToast('Please select quantity.', true); qty.focus(); return; }
    if (!location.value.trim()) { showToast('Please enter your city/state.', true); location.focus(); return; }

    const subject = encodeURIComponent('Scrap Selling Enquiry — ' + (type.options[type.selectedIndex]?.text || ''));
    const body = encodeURIComponent(
      'SCRAP SELLING ENQUIRY\n\n' +
      'Name: '      + name.value.trim()                                          + '\n' +
      'Phone: '     + phone.value.trim()                                         + '\n' +
      'Scrap Type: '+ (type.options[type.selectedIndex]?.text || '-')            + '\n' +
      'Quantity: '  + (qty.options[qty.selectedIndex]?.text  || '-')             + '\n' +
      'Location: '  + location.value.trim()                                      + '\n\n' +
      'Notes:\n'    + (scrapForm.querySelector('#scrap-notes')?.value || '-')
    );

    window.location.href = `mailto:skplasticworks@gmail.com?subject=${subject}&body=${body}`;
    showToast('✓ Opening mail app with your scrap details…');
    scrapForm.reset();
  });
}

/* ---------- Fade-in on scroll (subtle entrance animation) ---------- */
const fadeEls = document.querySelectorAll(
  '.product-card, .why-card, .process-step, .facility-card, .value-card, .contact-item, .industry-card, .benefit-card, .testimonial-card'
);

if (fadeEls.length > 0 && 'IntersectionObserver' in window) {
  const fadeObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.style.opacity  = '1';
        entry.target.style.transform = 'translateY(0)';
        fadeObserver.unobserve(entry.target);
      }
    });
  }, { threshold: 0.12 });

  fadeEls.forEach(el => {
    el.style.opacity   = '0';
    el.style.transform = 'translateY(24px)';
    el.style.transition = 'opacity 0.55s ease, transform 0.55s ease';
    fadeObserver.observe(el);
  });
}
