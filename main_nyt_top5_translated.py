import os
import json
import smtplib
import argparse
import locale
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
import time

import requests
from dotenv import load_dotenv
from google.cloud import translate_v2 as translate

# --- Helper functions remain mostly the same ---
# (I've included them all here for a complete, copy-pasteable file)

def load_configuration():
    """Loads settings from config.json and environment variables from .env."""
    load_dotenv()
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
    required_vars = ['NYT_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS', 'EMAIL_HOST_USER', 'EMAIL_HOST_PASSWORD']
    for var in required_vars:
        if not os.getenv(var):
            raise ValueError(f"Error: Missing required environment variable '{var}' in .env file.")
    return config

def get_top_stories(api_key, section, limit):
    """Fetches top stories for a single section with a retry mechanism."""
    api_url = f"https://api.nytimes.com/svc/topstories/v2/{section}.json"
    params = {'api-key': api_key}
    max_retries, base_delay = 3, 5
    for attempt in range(max_retries):
        try:
            response = requests.get(api_url, params=params)
            response.raise_for_status()
            data = response.json()
            if data.get('status') == 'OK' and data.get('results'):
                return data['results'][:limit]
            return []
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                if attempt < max_retries - 1:
                    wait_time = base_delay * (2 ** attempt)
                    print(f"  - Warning: Rate limit hit for '{section}'. Waiting {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    print(f"  - Error: Max retries reached for '{section}'. Skipping.")
                    return []
            else: raise e
        except requests.exceptions.RequestException as e:
            raise ConnectionError(f"Network error fetching '{section}': {e}")
    return []

def translate_structured_stories(story_data, target_language):
    """Translates a dictionary of stories, preserving the section structure."""
    print("  - Preparing stories for translation...")
    translate_client = translate.Client()
    texts_to_translate = []
    section_keys = list(story_data.keys())
    texts_to_translate.extend([key.replace('_', ' ').replace('/', ' & ').title() for key in section_keys])
    for section in section_keys:
        for story in story_data[section]:
            texts_to_translate.append(story.get('title', ''))
            texts_to_translate.append(story.get('abstract', ''))
    if not texts_to_translate: return {}
    
    print(f"  - Translating {len(texts_to_translate)} text elements to '{target_language}'...")
    results = translate_client.translate(texts_to_translate, target_language=target_language)
    
    translated_data = {}
    result_index = 0
    for i, original_section_key in enumerate(section_keys):
        translated_section_title = results[i]['translatedText']
        translated_stories_list = []
        for original_story in story_data[original_section_key]:
            story_title_index = len(section_keys) + result_index
            story_abstract_index = len(section_keys) + result_index + 1
            translated_story = original_story.copy()
            translated_story['title'] = results[story_title_index]['translatedText']
            translated_story['abstract'] = results[story_abstract_index]['translatedText']
            translated_stories_list.append(translated_story)
            result_index += 2
        translated_data[translated_section_title] = translated_stories_list
    print("  - Translation complete.")
    return translated_data

def format_date_for_locale(locale_str):
    """Formats the current date using the specified locale."""
    try:
        locale.setlocale(locale.LC_TIME, locale_str)
        return datetime.now().strftime("%B %d, %Y")
    except locale.Error:
        print(f"  - Warning: Locale '{locale_str}' not found. Falling back to default date format.")
        return datetime.now().strftime("%Y-%m-%d")
    finally:
        locale.setlocale(locale.LC_TIME, '')

def format_email_body(template_path, subscription, formatted_date, structured_stories):
    """Formats the HTML email body with fully translated content."""
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()
    stories_html = ""
    for section_title, stories in structured_stories.items():
        stories_html += f"<h2>{section_title}</h2>"
        for story in stories:
            stories_html += f"""
            <div class="story">
                <div class="story-title"><a href="{story['url']}">{story['title']}</a></div>
                <div class="story-abstract">{story['abstract']}</div>
                <div class="story-byline">{story['byline']}</div>
            </div>
            """
    return template.format(
        briefing_title=subscription['main_briefing_title_localized'],
        date=formatted_date,
        stories_html=stories_html,
        language_name=subscription['target_language_name']
    )

def send_email(subject, body, recipient_email):
    """Sends the HTML email to a single recipient."""
    sender_email = os.getenv('EMAIL_HOST_USER')
    password = os.getenv('EMAIL_HOST_PASSWORD')
    print(f"  - Preparing to send email to {recipient_email}...")
    smtp_server = "smtp.gmail.com"
    port = 587
    server = smtplib.SMTP(smtp_server, port)
    try:
        server.starttls()
        server.login(sender_email, password)
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = sender_email
        message["To"] = recipient_email
        message.attach(MIMEText(body, "html"))
        server.sendmail(sender_email, recipient_email, message.as_string())
        print(f"  - Email sent successfully to {recipient_email}.")
    except Exception as e:
        print(f"  - Error sending email: {e}")
    finally:
        server.quit()

def main():
    """Main function to loop through subscriptions and process each one."""
    parser = argparse.ArgumentParser(description="Fetches, translates, and emails a digest of NYT Top Stories for multiple recipients.")
    parser.add_argument('--dry-run', action='store_true', help="Run without sending emails, save each output to a separate HTML file.")
    args = parser.parse_args()

    if args.dry_run:
        print("\n*** DRY RUN MODE ACTIVATED: No emails will be sent. ***\n")
    
    print(f"--- NYT Daily Digest Service started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
    
    config = load_configuration()
    api_key = os.getenv('NYT_API_KEY')
    subscriptions = config.get("subscriptions", [])
    total_subscriptions = len(subscriptions)

    if not subscriptions:
        print("No subscriptions found in config.json. Exiting.")
        return

    # --- Main Loop: Process each subscription individually ---
    for i, subscription in enumerate(subscriptions):
        recipient = subscription.get("recipient_email", f"Subscription_{i+1}")
        print(f"\n--- Processing subscription {i+1}/{total_subscriptions} for: {recipient} ---")

        try:
            # 1. Fetch stories for all sections in this subscription
            all_stories_by_section = {}
            sections_to_fetch = subscription.get("api_sections", [])
            for section in sections_to_fetch:
                print(f"  - Fetching section: '{section}'...")
                stories = get_top_stories(api_key, section, subscription.get("max_stories_per_section", 5))
                if stories:
                    all_stories_by_section[section] = stories
                time.sleep(7) # Always be polite to the API

            if not all_stories_by_section:
                print("  - No stories found for this subscription. Skipping.")
                continue
            
            # 2. Translate stories for this subscription
            translated_stories = translate_structured_stories(all_stories_by_section, subscription['target_language'])
            
            # 3. Format date and email body
            formatted_date = format_date_for_locale(subscription['email_locale'])
            email_body = format_email_body('email_template.html', subscription, formatted_date, translated_stories)
            
            # 4. Create subject and send
            today_str_for_subject = datetime.now().strftime("%d.%m.%Y")
            email_subject = subscription['email_subject_template'].format(date=today_str_for_subject)
            
            if args.dry_run:
                # Sanitize email for a safe filename
                safe_filename = recipient.replace('@', '_').replace('.', '_')
                output_filename = f'dry_run_{safe_filename}.html'
                with open(output_filename, 'w', encoding='utf-8') as f:
                    f.write(email_body)
                print(f"  - DRY RUN: Email content for {recipient} saved to '{output_filename}'.")
            else:
                send_email(email_subject, email_body, recipient)

        except Exception as e:
            print(f"\n!! An error occurred while processing subscription for {recipient}: {e}")
            print("!! Continuing to the next subscription.")
    
    print(f"\n--- All {total_subscriptions} subscriptions processed. Service finished. ---")

if __name__ == "__main__":
    main()
