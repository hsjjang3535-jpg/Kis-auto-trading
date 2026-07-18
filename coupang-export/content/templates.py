POST_TEMPLATE = """\
<article class="coupang-review">
  <h1>{{ title }}</h1>
  <p class="intro">{{ intro }}</p>

  <section class="product-card">
    <img src="{{ product_image }}" alt="{{ product_name }}" loading="lazy" />
    <h2>{{ product_name }}</h2>
    <p class="price">가격: {{ product_price }}원</p>
    {% if is_rocket %}<p>로켓배송</p>{% endif %}
    {% if is_free_shipping %}<p>무료배송</p>{% endif %}
    <p><a href="{{ affiliate_url }}" rel="nofollow sponsored" target="_blank">쿠팡에서 최저가 확인하기</a></p>
  </section>

  <section class="review-body">
    {{ body_html | safe }}
  </section>

  <section class="pros-cons">
    <h3>장점</h3>
    <ul>{% for item in pros %}<li>{{ item }}</li>{% endfor %}</ul>
    <h3>단점</h3>
    <ul>{% for item in cons %}<li>{{ item }}</li>{% endfor %}</ul>
  </section>

  <section class="faq">
    <h3>자주 묻는 질문</h3>
    {% for q, a in faq %}
    <details>
      <summary>{{ q }}</summary>
      <p>{{ a }}</p>
    </details>
    {% endfor %}
  </section>

  <p class="disclosure">
    "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
  </p>
</article>
"""
