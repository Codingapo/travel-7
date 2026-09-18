let isLogin = !window.location.pathname.endsWith("/register");
let otpCountdownTimer = null;
let otpVerified = false;


 let isFirstLoad = true;
  let isAnimating = false;



const OTP_RESEND_WAIT = 90;

function startOtpCountdown() {
    const resendBtn = document.getElementById('otp-resend-btn');
    const timerText = document.getElementById('timer-text');
    const timerIcon = document.getElementById('timer-icon');
    if (!resendBtn || !timerText || !timerIcon) return;

    if (otpCountdownTimer) clearInterval(otpCountdownTimer);
    let remaining = OTP_RESEND_WAIT;
    resendBtn.classList.add('hidden');
    timerIcon.innerHTML = '<i class="fas fa-hourglass-half"></i>';
    timerText.textContent = 'Resend available in 0:90';
    otpCountdownTimer = setInterval(() => {
        remaining--;
        if (remaining <= 0) {
            clearInterval(otpCountdownTimer);
            otpCountdownTimer = null;
            timerText.textContent = 'You can now resend a new code.';
            timerIcon.innerHTML = '<i class="fas fa-check-circle"></i>';
            resendBtn.classList.remove('hidden');
        } else {
            const mins = Math.floor(remaining / 60);
            const secs = remaining % 60;
            timerText.textContent = `Resend available in ${mins}:${secs < 10 ? '0' + secs : secs}`;
        }
    }, 1000);
}

  function toggleAuth() {
    if (isAnimating) return;
    isAnimating = true;

    isLogin = !isLogin;
    const loginForm = document.getElementById('login-form');
    const signupForm = document.getElementById('signup-form');
    const otpForm = document.getElementById('otp-form');
    const authTitle = document.getElementById('auth-title');

    if (otpForm) otpForm.classList.add('hidden');

    // ---------- Helpers ----------
    function animateOut(element, callback) {
      element.classList.remove(
        'animate__lightSpeedInRight',
        'animate__fadeIn',
        'animate__zoomIn',
        'animate__bounceIn',
        'animate__slideInLeft',
        'animate__slideInRight'
      );

      element.classList.add('animate__animated', 'animate__fadeOut');

      element.addEventListener('animationend', function handler() {
        element.classList.add('hidden');
        element.classList.remove('animate__animated', 'animate__fadeOut');
        element.removeEventListener('animationend', handler);
        if (callback) callback();
      }, { once: true });
    }

    function animateIn(element, useSpecial = false) {
      element.classList.remove('hidden');

      // Clean previous classes
      element.classList.remove(
        'animate__animated',
        'animate__bounceIn',
        'animate__lightSpeedInRight',
        'animate__fadeIn'
      );

      // On toggle we use lightSpeed, on first load we handle separately
      if (!useSpecial) {
        element.classList.add('animate__animated', 'animate__lightSpeedInRight');

        element.addEventListener('animationend', function handler() {
          element.classList.remove('animate__animated', 'animate__lightSpeedInRight');
          isAnimating = false;
          element.removeEventListener('animationend', handler);
        }, { once: true });
      }
    }

    // ---------- Main Logic ----------
    if (isLogin) {
      if (!signupForm.classList.contains('hidden')) {
        animateOut(signupForm, () => animateIn(loginForm));
      } else {
        animateIn(loginForm);
      }
      authTitle.textContent = 'Welcome back! Please login.';
      history.replaceState({}, '', '/auth/login');
    } else {
      if (otpVerified) {
        isLogin = true;
        if (!signupForm.classList.contains('hidden')) {
          animateOut(signupForm, () => animateIn(loginForm));
        } else {
          animateIn(loginForm);
        }
        authTitle.textContent = 'Welcome back! Please login.';
        history.replaceState({}, '', '/auth/login');
      } else {
        if (!loginForm.classList.contains('hidden')) {
          animateOut(loginForm, () => animateIn(signupForm));
        } else {
          animateIn(signupForm);
        }
        authTitle.textContent = 'Create your account to start booking.';
        history.replaceState({}, '', '/auth/register');
      }
    }

    isFirstLoad = false;
    clearMessages();
  }

  // ========== FIRST LOAD ANIMATION ==========
document.addEventListener('DOMContentLoaded', function () {

    const loginForm = document.getElementById('login-form');
    const signupForm = document.getElementById('signup-form');

    if (signupForm && !signupForm.classList.contains('hidden')) {
        runFirstLoadAnimation(signupForm);
    }

    else if (loginForm && !loginForm.classList.contains('hidden')) {
        runFirstLoadAnimation(loginForm);
    }

});



function runFirstLoadAnimation(form) {
    isAnimating = true;

    // Remove previous animation classes and delays
    form.querySelectorAll('.animate__animated').forEach(el => {
        el.classList.remove(
            'animate__animated',
            'animate__slideInLeft',
            'animate__slideInRight',
            'animate__lightSpeedInLeft',
            'animate__lightSpeedInRight',
            'animate__zoomIn',
            'animate__flipInY',
            'animate__fadeIn'
        );

        el.style.animationDelay = '';
    });

    // Force browser reflow so Animate.css starts fresh
    void form.offsetWidth;

    /*
    ============================================================
    LOGIN FORM
    ============================================================
    */
    if (form.id === 'login-form') {

        // 1. Email → slide from LEFT
        const emailGroup = form.children[0];

        if (emailGroup) {
            emailGroup.classList.add(
                'animate__animated',
                'animate__slideInLeft'
            );

            emailGroup.style.animationDelay = '0.5s';

            // Envelope → zoomIn
            const emailIcon = emailGroup.querySelector('i');

            if (emailIcon) {
                emailIcon.classList.add(
                    'animate__animated',
                    'animate__zoomIn'
                );

                emailIcon.style.animationDelay = '1s';
            }
        }


        // 2. Password → slide from RIGHT
        const passwordGroup = form.children[1];

        if (passwordGroup) {
            passwordGroup.classList.add(
                'animate__animated',
                'animate__slideInRight'
            );

            passwordGroup.style.animationDelay = '1s';

            // Lock → zoomIn
            const lockIcon = passwordGroup.querySelector('i');

            if (lockIcon) {
                lockIcon.classList.add(
                    'animate__animated',
                    'animate__zoomIn'
                );

                lockIcon.style.animationDelay = '2.2s';
            }
        }


        // 3. Sign In button → zoomIn
        const signInBtn = form.querySelector(
            'button[type="submit"]'
        );

        if (signInBtn) {
            signInBtn.classList.add(
                'animate__animated',
                'animate__zoomIn'
            );

            signInBtn.style.animationDelay = '3s';
        }


        // 4. Forgot password → fadeIn
        const forgotLink = form.querySelector(
            'a[href*="forgot"]'
        );

        if (forgotLink) {
            forgotLink.classList.add(
                'animate__animated',
                'animate__fadeInUp'
            );

            forgotLink.style.animationDelay = '3.5s';
        }


        // 5. Bottom text → fadeIn
        const bottomText = form.querySelector('p');

        if (bottomText) {
            bottomText.classList.add(
                'animate__animated',
                'animate__fadeInLeft'
            );

            bottomText.style.animationDelay = '2.85s';
        }
    }


    /*
    ============================================================
    SIGNUP FORM
    ============================================================
    */
    else if (form.id === 'signup-form') {

        /*
        1. EMAIL
        slideInLeft
        */
        const emailGroup = form.children[0];

        if (emailGroup) {

            emailGroup.classList.add(
                'animate__animated',
                'animate__slideInLeft'
            );

            emailGroup.style.animationDelay = '0.5s';

            // Envelope → flipInY
            const emailIcon = emailGroup.querySelector('i');

            if (emailIcon) {
                emailIcon.classList.add(
                    'animate__animated',
                    'animate__flipInY'
                );

                emailIcon.style.animationDelay = '0.9s';
            }
        }


        /*
        2. PASSWORD
        lightSpeedInLeft
        */
        const passwordGroup = form.children[1];

        if (passwordGroup) {

            passwordGroup.classList.add(
                'animate__animated',
                'animate__lightSpeedInLeft'
            );

            passwordGroup.style.animationDelay = '1s';

            // Lock → flipInY
            const lockIcon = passwordGroup.querySelector('i');

            if (lockIcon) {
                lockIcon.classList.add(
                    'animate__animated',
                    'animate__flipInY'
                );

                lockIcon.style.animationDelay = '1.4s';
            }
        }


        /*
        3. CONFIRM PASSWORD
        slideInLeft
        */
        const confirmGroup = form.children[2];

        if (confirmGroup) {

            confirmGroup.classList.add(
                'animate__animated',
                'animate__slideInLeft'
            );

            confirmGroup.style.animationDelay = '2s';

            // Shield → flipInY
            const shieldIcon = confirmGroup.querySelector('i');

            if (shieldIcon) {
                shieldIcon.classList.add(
                    'animate__animated',
                    'animate__fadeInRight'
                );

                shieldIcon.style.animationDelay = '2.7s';
            }
        }


        /*
        4. REGISTER BUTTON
        flipInY
        */
        const registerBtn = form.querySelector(
            'button[type="submit"]'
        );

        if (registerBtn) {

            registerBtn.classList.add(
                'animate__animated',
                'animate__flipInY'
            );

            registerBtn.style.animationDelay = '3.1s';
        }


        /*
        5. BOTTOM TEXT
        flipInY
        */
        const bottomText = form.querySelector('p');

        if (bottomText) {

            bottomText.classList.add(
                'animate__animated',
                'animate__flipInY'
            );

            bottomText.style.animationDelay = '3.5s';
        }
    }


    /*
    ============================================================
    CLEANUP
    ============================================================
    Last signup animation starts at 3.5s.
    Animate.css default duration ≈ 1s.
    Therefore cleanup at 4.7s.
    ============================================================
    */

    setTimeout(() => {

        form.querySelectorAll('.animate__animated').forEach(el => {

            el.classList.remove(
                'animate__animated',
                'animate__slideInLeft',
                'animate__slideInRight',
                'animate__lightSpeedInLeft',
                'animate__lightSpeedInRight',
                'animate__zoomIn',
                'animate__flipInY',
                'animate__fadeIn'
            );

            el.style.animationDelay = '';
        });

        isAnimating = false;
        isFirstLoad = false;

    }, 4700);
}

function applyAuthRoute() {
    const register = window.location.pathname.endsWith('/register');
    isLogin = !register;
    if (otpVerified) isLogin = true;
    const loginForm = document.getElementById('login-form');
    const signupForm = document.getElementById('signup-form');
    const otpForm = document.getElementById('otp-form');
    const authTitle = document.getElementById('auth-title');
    if (!loginForm || !signupForm) return;
    loginForm.classList.toggle('hidden', !isLogin);
    signupForm.classList.toggle('hidden', isLogin);
    if (otpForm) otpForm.classList.add('hidden');
    authTitle.textContent = isLogin ? 'Welcome back! Please login.' : 'Create your account to start booking.';
}

function clearMessages() {
    const errorEl = document.getElementById('auth-error');
    const successEl = document.getElementById('auth-success');
    if (errorEl) errorEl.classList.add('hidden');
    if (successEl) successEl.classList.add('hidden');
}

function showMessage(type, text) {
    const errorEl = document.getElementById('auth-error');
    const successEl = document.getElementById('auth-success');
    
    clearMessages();
    if (type === 'error') {
        errorEl.textContent = text;
        errorEl.classList.remove('hidden');
    } else {
        successEl.textContent = text;
        successEl.classList.remove('hidden');
    }
}

function validateEmail(email) {
    return String(email)
        .toLowerCase()
        .match(/^(([^<>()[\]\\.,;:\s@"]+(\.[^<>()[\]\\.,;:\s@"]+)*)|(".+"))@((\[[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\])|(([a-zA-Z\-0-9]+\.)+[a-zA-Z]{2,}))$/);
}

async function forgotPasswordFlow() {
    const email = prompt('Enter your account email or username:');
    if (!email) return;
    try {
        const req = await fetch('/api/auth/forgot-password/request', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password: 'ignored' })
        });
        const payload = await req.json();
        if (!payload.success) {
            showMessage('error', payload.error || 'Unable to request OTP');
            return;
        }
        const otpFromServer = payload?.data?.otp || '';
        const otp = prompt(`Enter OTP sent to your account${otpFromServer ? ` (Dev OTP: ${otpFromServer})` : ''}:`);
        if (!otp) return;
        const newPassword = prompt('Enter your new password (min 12 chars, must include uppercase, lowercase, number, and special character):');
        if (!newPassword) return;
        const validation = typeof validatePasswordStrength === 'function'
            ? validatePasswordStrength(newPassword)
            : { valid: newPassword.length >= 12 };
        if (!validation.valid) {
            showMessage('error', validation.errors?.[0] || 'New password does not meet security requirements.');
            return;
        }
        const verify = await fetch('/api/auth/forgot-password/verify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, otp, password: newPassword })
        });
        const verifyPayload = await verify.json();
        if (verifyPayload.success) {
            showMessage('success', 'Password reset successful. You can login now.');
        } else {
            showMessage('error', verifyPayload.error || verifyPayload.detail || 'OTP verification failed');
        }
    } catch (error) {
        showMessage('error', 'Password reset failed. Please try again.');
    }
}

// Login Handler
document.getElementById('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const email = document.getElementById('login-email').value.trim();
    const password = document.getElementById('login-password').value;

    if (!validateEmail(email)) {
        showMessage('error', 'Please enter a valid email address.');
        return;
    }

    if (!password) {
        showMessage('error', 'Please enter your password.');
        return;
    }

    try {
        const response = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password })
        });
        const data = await response.json();

        if (data.success) {
            localStorage.setItem('token', data.token);
            localStorage.setItem('user', JSON.stringify(data.user));
            showMessage('success', 'Login successful! Redirecting...');
            setTimeout(() => {
                window.location.href = data.redirect || '/home';
            }, 1000);
        } else {
            showMessage('error', data.error);
        }
    } catch (error) {
        showMessage('error', 'An error occurred. Please try again.');
    }
});

// Signup Handler
document.getElementById('signup-form').addEventListener('submit', async (e) => {
    e.preventDefault();

    const email = document.getElementById('signup-email').value.trim();
    const password = document.getElementById('signup-password').value;
    const confirm = document.getElementById('signup-confirm').value;


    if (!validateEmail(email)) {
        showMessage('error', 'Please enter a valid email address.');
        return;
    }

    const emailLocal = email.includes('@') ? email.split('@')[0] : email;
    const validation = validatePasswordStrength(password, [email, emailLocal]);
    if (!validation.valid) {
        showMessage('error', validation.errors[0] || 'Please choose a stronger password.');
        return;
    }


    if (password !== confirm) {
        showMessage('error', 'Passwords do not match.');
        return;
    }


    try {

        const response = await fetch('/api/auth/register', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                email,
                password
            })
        });


        const data = await response.json();


if (data.success) {

            pendingEmail = email;

            document.getElementById('signup-form').classList.add('hidden');
            document.getElementById('otp-form').classList.remove('hidden');

            showMessage(
                'success',
                'Verification code sent. Check your email.'
            );

            startOtpCountdown();

        } else {

            showMessage(
                'error',
                data.error || data.detail
            );

        }


    } catch(error){

        showMessage(
            'error',
            'Unable to create account. Try again.'
        );

    }

});


// Resend registration OTP
document.getElementById('otp-resend-btn').addEventListener('click', async () => {
    const btn = document.getElementById('otp-resend-btn');
    if (!pendingEmail) return;
    btn.disabled = true;
    btn.textContent = 'Sending new code...';
    try {
        const resp = await fetch('/api/auth/register/resend', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: pendingEmail, password: '' })
        });
        const data = await resp.json();
        if (!data.success) {
            showMessage('error', data.error || 'Unable to resend verification code.');
            return;
        }
        document.getElementById('otp-code').value = '';
        document.getElementById('otp-code').focus();
        showMessage('success', 'A new verification code has been sent to your email.');
        startOtpCountdown();
    } catch (error) {
        showMessage('error', 'Unable to resend the verification code.');
    } finally {
        btn.disabled = false;
        btn.textContent = "Didn't receive the code? Send again";
    }
});


document.getElementById('otp-form').addEventListener('submit', async (e)=>{

    e.preventDefault();


const otp = document.getElementById('otp-code').value.trim();
    try {

        const response = await fetch('/api/auth/verify-registration',{

            method:'POST',

            headers:{
                'Content-Type':'application/json'
            },

            body:JSON.stringify({

                email: pendingEmail,

                otp

            })

        });


        const data = await response.json();


        if(data.success){

            otpVerified = true;
            showMessage(
                'success',
                'Account verified. You can now login.'
            );

            pendingEmail = "";
            if (otpCountdownTimer) {
                clearInterval(otpCountdownTimer);
                otpCountdownTimer = null;
            }

            const otpForm = document.getElementById('otp-form');
            const loginForm = document.getElementById('login-form');
            const signupForm = document.getElementById('signup-form');
            const authTitle = document.getElementById('auth-title');
            const otpCodeInput = document.getElementById('otp-code');

            if (otpForm) otpForm.classList.add('hidden');
            if (signupForm) signupForm.classList.add('hidden');
            if (otpCodeInput) otpCodeInput.value = '';
            if (loginForm) loginForm.classList.remove('hidden');
            if (authTitle) authTitle.textContent = 'Welcome back! Please login.';

            isLogin = true;
            history.replaceState({}, '', '/auth/login');


        }else{

            showMessage(
                'error',
                data.error || data.detail
            );

        }


    }catch(error){

        showMessage(
            'error',
            'Verification failed. Try again.'
        );

    }

});


document.addEventListener("DOMContentLoaded", applyAuthRoute);
