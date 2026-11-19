const form = document.querySelector('#review-form');
const stars = [...form.querySelectorAll('[name=rating]')];
for (const radio of stars) radio.addEventListener('change', () => {
  for (const star of stars) star.closest('label').classList.toggle('selected', Number(star.value) <= Number(radio.value));
  document.querySelector('#rating-value').textContent = `${radio.value} out of 5 stars`;
});
async function request(url, options) {
  const response = await fetch(url, options);
  const data = response.status === 204 ? null : await response.json();
  if (!response.ok) throw new Error(typeof data?.detail === 'string' ? data.detail : 'Unable to complete this request. Please check your details and try again.');
  return data;
}
async function loadReviews() {
  const status = document.querySelector('#reviews-status');
  const retry = document.querySelector('#retry-reviews');
  retry.hidden = true;
  status.textContent = 'Loading reviews…';
  try {
    const reviews = await request('/api/reviews');
    const list = document.querySelector('#public-reviews');
    list.replaceChildren();
    for (const review of reviews) {
      const card = document.createElement('article'); card.className = 'booking-state';
      const name = document.createElement('h3'); name.textContent = review.guest_name;
      const rating = document.createElement('p'); rating.className = 'review-stars';
      rating.textContent = '★'.repeat(review.rating) + '☆'.repeat(5 - review.rating);
      rating.setAttribute('aria-label', `${review.rating} out of 5 stars`);
      const comment = document.createElement('p'); comment.textContent = review.comment;
      card.append(name, rating, comment); list.append(card);
    }
    status.textContent = reviews.length ? 'Approved guest experiences' : 'No published reviews yet. Be the first to share your stay.';
  } catch { status.textContent = 'Reviews could not be loaded. Please try again.'; retry.hidden = false; }
}
document.querySelector('#retry-reviews').addEventListener('click', loadReviews);
form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = form.querySelector('[type=submit]');
  const feedback = document.querySelector('#review-feedback');
  button.disabled = true; feedback.textContent = 'Submitting your review…';
  const values = Object.fromEntries(new FormData(form)); values.rating = Number(values.rating);
  values.booking_ref = values.booking_ref.trim(); values.email = values.email.trim();
  try {
    await request('/api/reviews', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values)});
    feedback.textContent = 'Thank you! Your review has been sent to the resort and will appear after approval.';
    button.textContent = 'Review submitted';
  } catch (error) { feedback.textContent = error.message; button.disabled = false; }
});
loadReviews();
