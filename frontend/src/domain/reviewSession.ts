import { reactive } from 'vue'

const storedUser = typeof window !== 'undefined' ? window.localStorage.getItem('review-current-user') : ''

export const reviewSession = reactive({
  currentUser: storedUser || '甲',
  m72KeyA: 'UI-A-M72-600G-001',
  m72KeyB: 'UI-B-M72-600G-001',
})

export function setReviewUser(user: string) {
  reviewSession.currentUser = user
  if (typeof window !== 'undefined') {
    window.localStorage.setItem('review-current-user', user)
  }
}

export function notifyReviewFlowChanged() {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('review-flow-changed'))
  }
}
