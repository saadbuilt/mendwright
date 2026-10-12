"""Words for the synthetic pages: UI concepts with their English synonyms and their
translations, plus entity names (products, people, projects, brands).

A concept is one control meaning, such as "sign in" or "add to cart". Two controls with
the same concept are the same action, whatever words they use, and two controls with
different concepts are different actions, however alike their words look (Save draft and
Delete draft). The generator relies on that: a reworded or translated control keeps its
concept, and a decoy never shares the target's concept.

Each entry: kind | slugs | en | de | fr | es | pt | it | nl, with "/" between variants.
kind is button, link, input:<type>, checkbox, radio, select, textarea or switch. slugs are
the words ids, names and test ids are made from. An empty language falls back to English.
"""

LANGS = ('en', 'de', 'fr', 'es', 'pt', 'it', 'nl')

_RAW = """
sign_in|button|sign-in/login/signin/log-in|Sign in/Log in/Login/Sign into your account|Anmelden/Einloggen|Se connecter/Connexion|Iniciar sesión/Entrar|Entrar/Iniciar sessão|Accedi|Inloggen/Aanmelden
sign_in_link|link|sign-in/login/signin|Sign in/Log in/Already have an account? Sign in|Anmelden/Einloggen|Se connecter/Connexion|Iniciar sesión|Entrar|Accedi|Inloggen
sign_up|button|sign-up/register/create-account/signup|Create account/Sign up/Register/Create my account/Join now|Konto erstellen/Registrieren|Créer un compte/S'inscrire|Crear cuenta/Registrarse|Criar conta/Cadastrar-se|Crea account/Registrati|Account aanmaken/Registreren
sign_up_link|link|sign-up/register/signup|Sign up/Create an account/Register|Registrieren/Konto erstellen|S'inscrire/Créer un compte|Regístrate/Crear cuenta|Cadastre-se/Criar conta|Registrati|Registreren
sign_out|button|sign-out/logout/log-out|Sign out/Log out|Abmelden/Ausloggen|Se déconnecter/Déconnexion|Cerrar sesión|Sair/Terminar sessão|Esci/Disconnetti|Uitloggen/Afmelden
forgot_password|link|forgot-password/reset-password/forgot|Forgot password?/Forgot your password?/Reset password|Passwort vergessen?|Mot de passe oublié ?|¿Olvidaste tu contraseña?|Esqueceu a senha?|Password dimenticata?|Wachtwoord vergeten?
email|input:email|email/email-address/user-email/login-email|Email/Email address/E-mail/Work email|E-Mail/E-Mail-Adresse|E-mail/Adresse e-mail|Correo electrónico/Email|E-mail/Endereço de e-mail|Email/Indirizzo email|E-mail/E-mailadres
password|input:password|password/pass/pwd/login-password|Password/Your password|Passwort/Kennwort|Mot de passe|Contraseña|Senha|Password|Wachtwoord
confirm_password|input:password|confirm-password/password-confirm/repeat-password/password2|Confirm password/Repeat password/Re-enter password|Passwort bestätigen/Passwort wiederholen|Confirmer le mot de passe|Confirmar contraseña/Repetir contraseña|Confirmar senha|Conferma password|Bevestig wachtwoord/Herhaal wachtwoord
username|input:text|username/user-name/login|Username/User name|Benutzername|Nom d'utilisateur|Nombre de usuario|Nome de usuário|Nome utente|Gebruikersnaam
remember_me|checkbox|remember-me/remember/keep-signed-in|Remember me/Keep me signed in/Stay signed in|Angemeldet bleiben|Se souvenir de moi/Rester connecté|Recordarme/Mantener la sesión iniciada|Lembrar de mim/Manter conectado|Ricordami|Onthoud mij/Ingelogd blijven
full_name|input:text|full-name/name/fullname|Full name/Name/Your name|Vollständiger Name/Name|Nom complet|Nombre completo|Nome completo|Nome completo|Volledige naam
first_name|input:text|first-name/firstname/given-name/fname|First name/Given name|Vorname|Prénom|Nombre|Nome/Primeiro nome|Nome|Voornaam
last_name|input:text|last-name/lastname/family-name/lname|Last name/Surname/Family name|Nachname|Nom de famille/Nom|Apellidos/Apellido|Sobrenome/Apelido|Cognome|Achternaam
phone|input:tel|phone/phone-number/tel/mobile|Phone/Phone number/Mobile number|Telefon/Telefonnummer|Téléphone/Numéro de téléphone|Teléfono/Número de teléfono|Telefone/Celular|Telefono/Numero di telefono|Telefoonnummer/Mobiel nummer
company|input:text|company/company-name/organization/org|Company/Company name/Organization|Firma/Unternehmen|Entreprise/Société|Empresa/Nombre de la empresa|Empresa/Nome da empresa|Azienda/Nome azienda|Bedrijf/Bedrijfsnaam
accept_terms|checkbox|terms/accept-terms/agree-terms/tos|I agree to the Terms of Service/I accept the terms and conditions/I agree to the terms|Ich akzeptiere die AGB/Ich stimme den Nutzungsbedingungen zu|J'accepte les conditions d'utilisation|Acepto los términos y condiciones|Aceito os termos de uso|Accetto i termini e le condizioni|Ik ga akkoord met de voorwaarden
newsletter_optin|checkbox|newsletter/marketing-opt-in/opt-in/updates|Send me product updates/Subscribe to our newsletter/Email me news and offers|Newsletter abonnieren/Ich möchte den Newsletter erhalten|Recevoir la newsletter/M'inscrire à la newsletter|Suscribirme al boletín/Quiero recibir novedades|Quero receber novidades/Assinar a newsletter|Iscrivimi alla newsletter|Schrijf me in voor de nieuwsbrief
continue|button|continue/next/proceed/next-step|Continue/Next/Proceed/Next step|Weiter/Fortfahren|Continuer/Suivant|Continuar/Siguiente|Continuar/Próximo|Continua/Avanti|Doorgaan/Volgende
back|button|back/previous/prev/go-back|Back/Go back/Previous|Zurück|Retour/Précédent|Atrás/Volver|Voltar|Indietro|Terug/Vorige
cancel|button|cancel/dismiss|Cancel/Never mind|Abbrechen|Annuler|Cancelar|Cancelar|Annulla|Annuleren
google_sign_in|button|google/google-sign-in/oauth-google|Continue with Google/Sign in with Google|Weiter mit Google/Mit Google anmelden|Continuer avec Google|Continuar con Google|Continuar com o Google|Continua con Google|Doorgaan met Google
apple_sign_in|button|apple/apple-sign-in/oauth-apple|Continue with Apple/Sign in with Apple|Weiter mit Apple|Continuer avec Apple|Continuar con Apple|Continuar com a Apple|Continua con Apple|Doorgaan met Apple
show_password|button|show-password/toggle-password/reveal|Show password/Show|Passwort anzeigen|Afficher le mot de passe|Mostrar contraseña|Mostrar senha|Mostra password|Wachtwoord tonen
send_reset_link|button|send-reset/reset-submit/send-link|Send reset link/Email me a reset link/Reset password|Link zum Zurücksetzen senden|Envoyer le lien de réinitialisation|Enviar enlace de restablecimiento|Enviar link de redefinição|Invia link di reimpostazione|Resetlink versturen
verify_code|input:text|code/otp/verification-code/one-time-code|Verification code/6-digit code/Enter code|Bestätigungscode|Code de vérification|Código de verificación|Código de verificação|Codice di verifica|Verificatiecode
verify|button|verify/confirm-code/submit-code|Verify/Confirm code/Verify code|Bestätigen/Verifizieren|Vérifier|Verificar|Verificar|Verifica|Verifiëren
resend_code|button|resend/resend-code|Resend code/Send a new code|Code erneut senden|Renvoyer le code|Reenviar código|Reenviar código|Invia di nuovo il codice|Code opnieuw verzenden
add_to_cart|button|add-to-cart/add-cart/atc/add-to-bag|Add to cart/Add to bag/Add to basket|In den Warenkorb/Zum Warenkorb hinzufügen|Ajouter au panier|Añadir al carrito/Agregar al carrito|Adicionar ao carrinho|Aggiungi al carrello|In winkelwagen/Toevoegen aan winkelwagen
add_to_wishlist|button|wishlist/add-to-wishlist/favorite/save-item|Add to wishlist/Save to wishlist/Add to favorites|Auf die Wunschliste/Zur Merkliste hinzufügen|Ajouter à la liste de souhaits/Ajouter aux favoris|Añadir a la lista de deseos/Añadir a favoritos|Adicionar à lista de desejos/Favoritar|Aggiungi alla lista dei desideri|Op verlanglijst/Toevoegen aan favorieten
buy_now|button|buy-now/buy/instant-buy|Buy now/Buy it now|Jetzt kaufen|Acheter maintenant|Comprar ahora|Comprar agora|Acquista ora|Nu kopen
checkout|button|checkout/proceed-checkout/go-checkout/to-checkout|Checkout/Proceed to checkout/Go to checkout/Check out|Zur Kasse/Weiter zur Kasse|Passer à la caisse/Valider mon panier|Tramitar pedido/Ir a pagar|Finalizar compra/Ir para o pagamento|Procedi all'acquisto/Vai alla cassa|Afrekenen/Naar de kassa
place_order|button|place-order/complete-order/submit-order/confirm-order|Place order/Complete order/Confirm order/Complete purchase|Zahlungspflichtig bestellen/Bestellung abschicken|Passer la commande/Confirmer la commande|Realizar pedido/Confirmar pedido|Fazer pedido/Confirmar pedido|Effettua ordine/Conferma ordine|Bestelling plaatsen/Bestellen
apply_coupon|button|apply-coupon/apply-code/redeem/apply-discount|Apply/Apply code/Redeem|Einlösen/Anwenden|Appliquer|Aplicar|Aplicar|Applica|Toepassen
remove_coupon|button|remove-coupon/remove-code/clear-code|Remove code/Remove discount|Code entfernen|Supprimer le code|Quitar código|Remover cupom|Rimuovi codice|Code verwijderen
coupon_code|input:text|coupon/promo/discount-code/promo-code/voucher|Discount code/Coupon code/Promo code/Gift card or discount code|Gutscheincode/Rabattcode|Code promo/Code de réduction|Código de descuento/Código promocional|Cupom de desconto/Código promocional|Codice sconto/Codice promozionale|Kortingscode/Actiecode
quantity|input:number|quantity/qty/item-qty|Quantity/Qty|Menge/Anzahl|Quantité|Cantidad|Quantidade|Quantità|Aantal
remove_item|button|remove/remove-item/delete-item|Remove/Remove item|Entfernen|Retirer/Supprimer l'article|Quitar/Eliminar artículo|Remover/Remover item|Rimuovi|Verwijderen
save_for_later|button|save-for-later/save-later/move-to-wishlist|Save for later/Move to wishlist|Für später speichern|Enregistrer pour plus tard|Guardar para más tarde|Salvar para depois|Salva per dopo|Bewaren voor later
continue_shopping|link|continue-shopping/keep-shopping/back-to-shop|Continue shopping/Keep shopping/Back to shop|Weiter einkaufen|Continuer mes achats|Seguir comprando|Continuar comprando|Continua lo shopping|Verder winkelen
update_cart|button|update-cart/update-basket/recalculate|Update cart/Update basket/Recalculate|Warenkorb aktualisieren|Mettre à jour le panier|Actualizar carrito|Atualizar carrinho|Aggiorna carrello|Winkelwagen bijwerken
empty_cart|button|empty-cart/clear-cart/remove-all|Empty cart/Clear cart/Remove all items|Warenkorb leeren|Vider le panier|Vaciar carrito|Esvaziar carrinho|Svuota carrello|Winkelwagen legen
size|select|size/size-select/variant-size|Size/Choose a size/Select size|Größe/Größe wählen|Taille/Choisir une taille|Talla/Elige tu talla|Tamanho/Escolha o tamanho|Taglia/Seleziona la taglia|Maat/Kies je maat
color|select|color/colour/variant-color|Color/Colour/Choose a color|Farbe|Couleur|Color|Cor|Colore|Kleur
address|input:text|address/address1/street/address-line1|Address/Street address/Address line 1|Adresse/Straße und Hausnummer|Adresse|Dirección|Endereço/Morada|Indirizzo|Adres/Straat en huisnummer
address2|input:text|address2/apartment/address-line2/suite|Apartment, suite, etc. (optional)/Address line 2|Adresszusatz|Complément d'adresse|Apartamento, piso, etc.|Complemento|Interno, scala (facoltativo)|Toevoeging
city|input:text|city/town/locality|City/Town or city|Stadt/Ort|Ville|Ciudad|Cidade|Città|Plaats/Woonplaats
postcode|input:text|postcode/zip/postal-code/zipcode|Postcode/ZIP code/Postal code|Postleitzahl/PLZ|Code postal|Código postal|CEP/Código postal|CAP|Postcode
country|select|country/country-select/region|Country/Country or region|Land|Pays|País|País|Paese|Land
state|select|state/province/region-select|State/State or province/Region|Bundesland|Région|Provincia|Estado|Provincia|Provincie
same_as_billing|checkbox|same-as-billing/billing-same/use-shipping|Billing address same as shipping/Use shipping address for billing|Rechnungsadresse entspricht Lieferadresse|Adresse de facturation identique à la livraison|La dirección de facturación es la misma|Endereço de cobrança igual ao de entrega|L'indirizzo di fatturazione è uguale|Factuuradres is gelijk aan verzendadres
ship_standard|radio|shipping-standard/standard/ship-std|Standard shipping/Standard delivery|Standardversand|Livraison standard|Envío estándar|Entrega padrão|Spedizione standard|Standaard verzending
ship_express|radio|shipping-express/express/ship-exp|Express shipping/Express delivery/Next-day delivery|Expressversand|Livraison express|Envío exprés|Entrega expressa|Spedizione espressa|Expresverzending
pay_card|radio|pay-card/card/credit-card|Credit card/Card/Debit or credit card|Kreditkarte|Carte bancaire|Tarjeta|Cartão de crédito|Carta di credito|Creditcard
pay_paypal|radio|pay-paypal/paypal|PayPal|PayPal|PayPal|PayPal|PayPal|PayPal|PayPal
sort_by|select|sort/sort-by/order-by|Sort by/Sort|Sortieren nach|Trier par|Ordenar por|Ordenar por|Ordina per|Sorteren op
apply_filters|button|apply-filters/filter-apply/show-results|Apply filters/Show results/Apply|Filter anwenden|Appliquer les filtres|Aplicar filtros|Aplicar filtros|Applica filtri|Filters toepassen
clear_filters|button|clear-filters/reset-filters/clear-all|Clear all/Clear filters/Reset filters|Filter zurücksetzen/Alle löschen|Effacer les filtres/Tout effacer|Borrar filtros|Limpar filtros|Cancella filtri|Filters wissen
next_page|link|next/next-page/pagination-next|Next/Next page|Weiter/Nächste Seite|Suivant/Page suivante|Siguiente|Próxima|Successiva|Volgende
prev_page|link|prev/previous/prev-page/pagination-prev|Previous/Previous page|Zurück/Vorherige Seite|Précédent/Page précédente|Anterior|Anterior|Precedente|Vorige
load_more|button|load-more/show-more/more|Load more/Show more|Mehr laden/Mehr anzeigen|Charger plus/Voir plus|Cargar más|Carregar mais|Carica altri|Meer laden
search|input:search|search/q/search-input/query/site-search|Search/Search products/What are you looking for?|Suchen/Suche|Rechercher|Buscar|Pesquisar/Buscar|Cerca|Zoeken
search_button|button|search-button/search-submit/do-search|Search/Go|Suchen|Rechercher|Buscar|Pesquisar|Cerca|Zoeken
view_cart|link|cart/view-cart/bag/basket|Cart/View cart/Bag/Basket|Warenkorb|Panier|Carrito/Cesta|Carrinho|Carrello|Winkelwagen
filter_option|checkbox|filter/facet/brand-filter/category-filter|Filter/Brand/Category|Filter|Filtre|Filtro|Filtro|Filtro|Filter
in_stock|checkbox|in-stock/available/stock-only|In stock only/Hide out of stock items|Nur verfügbare Artikel|En stock uniquement|Solo en stock|Somente em estoque|Solo disponibili|Alleen op voorraad
save_changes|button|save/save-changes/update-profile/save-settings|Save changes/Save/Update profile/Save settings|Änderungen speichern/Speichern|Enregistrer les modifications/Enregistrer|Guardar cambios/Guardar|Salvar alterações/Salvar|Salva modifiche/Salva|Wijzigingen opslaan/Opslaan
save_address|button|save-address/address-save|Save address/Save this address|Adresse speichern|Enregistrer l'adresse|Guardar dirección|Salvar endereço|Salva indirizzo|Adres opslaan
save_payee|button|save-payee/payee-save/add-payee|Save payee/Save recipient|Empfänger speichern|Enregistrer le bénéficiaire|Guardar beneficiario|Salvar favorecido|Salva beneficiario|Begunstigde opslaan
display_name|input:text|display-name/public-name/nickname|Display name/Public name|Anzeigename|Nom d'affichage|Nombre visible|Nome de exibição|Nome visualizzato|Weergavenaam
bio|textarea|bio/about/biography/about-me|Bio/About you/Short bio|Über mich/Kurzbiografie|Biographie/À propos de vous|Biografía/Sobre ti|Biografia/Sobre você|Biografia|Bio/Over jou
language|select|language/locale/lang|Language/Display language|Sprache|Langue|Idioma|Idioma|Lingua|Taal
timezone|select|timezone/time-zone/tz|Time zone/Timezone|Zeitzone|Fuseau horaire|Zona horaria|Fuso horário|Fuso orario|Tijdzone
email_notifications|switch|email-notifications/notify-email/email-alerts|Email notifications/Send me email notifications|E-Mail-Benachrichtigungen|Notifications par e-mail|Notificaciones por correo|Notificações por e-mail|Notifiche via email|E-mailmeldingen
push_notifications|switch|push-notifications/notify-push/push|Push notifications/Mobile push notifications|Push-Benachrichtigungen|Notifications push|Notificaciones push|Notificações push|Notifiche push|Pushmeldingen
weekly_digest|checkbox|weekly-digest/digest/weekly-summary|Weekly digest/Weekly summary email|Wöchentliche Zusammenfassung|Résumé hebdomadaire|Resumen semanal|Resumo semanal|Riepilogo settimanale|Wekelijkse samenvatting
dark_mode|switch|dark-mode/theme-dark/dark-theme|Dark mode/Dark theme|Dunkelmodus|Mode sombre|Modo oscuro|Modo escuro|Modalità scura|Donkere modus
two_factor|switch|two-factor/2fa/mfa|Two-factor authentication/Enable 2FA|Zwei-Faktor-Authentifizierung|Authentification à deux facteurs|Autenticación en dos pasos|Autenticação de dois fatores|Autenticazione a due fattori|Tweestapsverificatie
change_password|button|change-password/update-password|Change password/Update password|Passwort ändern|Changer le mot de passe|Cambiar contraseña|Alterar senha|Cambia password|Wachtwoord wijzigen
delete_account|button|delete-account/close-account/remove-account|Delete account/Delete my account/Close account|Konto löschen|Supprimer le compte|Eliminar cuenta|Excluir conta|Elimina account|Account verwijderen
export_data|button|export-data/download-data/data-export|Export data/Download my data|Daten exportieren|Exporter les données|Exportar datos|Exportar dados|Esporta dati|Gegevens exporteren
upload_avatar|button|upload-avatar/change-avatar/upload-photo|Upload photo/Change avatar/Upload new picture|Foto hochladen|Importer une photo|Subir foto|Enviar foto|Carica foto|Foto uploaden
remove_avatar|button|remove-avatar/delete-photo|Remove photo/Delete photo|Foto entfernen|Supprimer la photo|Eliminar foto|Remover foto|Rimuovi foto|Foto verwijderen
discard|button|discard/discard-changes|Discard changes/Discard|Änderungen verwerfen/Verwerfen|Ignorer les modifications|Descartar cambios|Descartar alterações|Scarta modifiche|Wijzigingen negeren
edit|button|edit/edit-row/edit-member|Edit/Edit member/Edit details|Bearbeiten|Modifier|Editar|Editar|Modifica|Bewerken
delete|button|delete/remove/trash|Delete/Remove|Löschen|Supprimer|Eliminar|Excluir|Elimina|Verwijderen
view|button|view/open/details|View/View details/Open|Ansehen/Details|Voir/Afficher|Ver/Ver detalles|Ver/Detalhes|Visualizza/Dettagli|Bekijken
duplicate|button|duplicate/copy/clone|Duplicate/Make a copy|Duplizieren|Dupliquer|Duplicar|Duplicar|Duplica|Dupliceren
archive|button|archive/archive-item|Archive/Move to archive|Archivieren|Archiver|Archivar|Arquivar|Archivia|Archiveren
download|button|download/download-file|Download/Download file|Herunterladen|Télécharger|Descargar|Baixar|Scarica|Downloaden
invite|button|invite/invite-member/add-member|Invite member/Invite people/Add member/Invite|Mitglied einladen|Inviter un membre|Invitar miembro|Convidar membro|Invita membro|Lid uitnodigen
export_csv|button|export/export-csv/download-csv|Export CSV/Export/Download CSV|Als CSV exportieren|Exporter en CSV|Exportar CSV|Exportar CSV|Esporta CSV|Exporteren als CSV
select_all|checkbox|select-all/check-all/toggle-all|Select all/Select all rows|Alle auswählen|Tout sélectionner|Seleccionar todo|Selecionar tudo|Seleziona tutto|Alles selecteren
rows_per_page|select|rows-per-page/page-size/per-page|Rows per page/Show|Zeilen pro Seite|Lignes par page|Filas por página|Linhas por página|Righe per pagina|Rijen per pagina
filter_status|select|status/status-filter/filter-status|Status/Filter by status|Status|Statut|Estado|Status|Stato|Status
search_table|input:search|search-members/filter-name/member-search|Search members/Filter by name/Find a member|Mitglieder suchen|Rechercher des membres|Buscar miembros|Buscar membros|Cerca membri|Leden zoeken
bulk_delete|button|bulk-delete/delete-selected|Delete selected|Ausgewählte löschen|Supprimer la sélection|Eliminar seleccionados|Excluir selecionados|Elimina selezionati|Geselecteerde verwijderen
new_project|button|new-project/create-project/add-project|New project/Create project/+ New project|Neues Projekt|Nouveau projet|Nuevo proyecto|Novo projeto|Nuovo progetto|Nieuw project
create_report|button|create-report/new-report|Create report/New report|Bericht erstellen|Créer un rapport|Crear informe|Criar relatório|Crea report|Rapport maken
refresh|button|refresh/reload|Refresh/Reload|Aktualisieren|Actualiser|Actualizar|Atualizar|Aggiorna|Vernieuwen
date_range|select|date-range/period/range|Date range/Period|Zeitraum|Période|Periodo|Período|Periodo|Periode
view_all|link|view-all/see-all|View all/See all|Alle anzeigen|Tout voir|Ver todo|Ver tudo|Vedi tutto|Alles bekijken
view_report|link|view-report/open-report/report|View report/Open report/See details|Bericht ansehen|Voir le rapport|Ver informe|Ver relatório|Vedi report|Rapport bekijken
notifications|button|notifications/notification-bell/alerts|Notifications/Open notifications|Benachrichtigungen|Notifications|Notificaciones|Notificações|Notifiche|Meldingen
account_menu|button|account-menu/user-menu/avatar-menu|Account menu/Open user menu/Your profile|Kontomenü|Menu du compte|Menú de cuenta|Menu da conta|Menu account|Accountmenu
upgrade|button|upgrade/upgrade-plan|Upgrade/Upgrade plan|Upgrade/Tarif upgraden|Mettre à niveau|Mejorar plan|Fazer upgrade|Esegui l'upgrade|Upgraden
skip|button|skip/skip-step/not-now|Skip/Skip for now/Not now|Überspringen|Passer/Ignorer pour l'instant|Omitir/Saltar|Pular|Salta|Overslaan
finish|button|finish/done/complete-setup|Finish/Done/Complete setup|Fertigstellen/Fertig|Terminer|Finalizar|Concluir|Fine/Completa|Voltooien/Klaar
team_size|select|team-size/company-size/headcount|Team size/How many people are on your team?|Teamgröße|Taille de l'équipe|Tamaño del equipo|Tamanho da equipe|Dimensione del team|Teamgrootte
job_role|select|role/job-role/job-title|Your role/Job title/What do you do?|Ihre Rolle|Votre rôle|Tu función|Sua função|Il tuo ruolo|Jouw rol
workspace_name|input:text|workspace/workspace-name/team-name|Workspace name/Team name|Name des Arbeitsbereichs|Nom de l'espace de travail|Nombre del espacio de trabajo|Nome do espaço de trabalho|Nome dell'area di lavoro|Naam van de werkruimte
use_work|radio|use-work/work/for-work|For work/Work|Für die Arbeit|Pour le travail|Para el trabajo|Para o trabalho|Per lavoro|Voor werk
use_personal|radio|use-personal/personal/for-me|Personal use/For myself|Privat|Usage personnel|Uso personal|Uso pessoal|Uso personale|Persoonlijk gebruik
confirm|button|confirm/ok/yes/confirm-button|Confirm/OK/Yes, continue|Bestätigen/OK|Confirmer/OK|Confirmar/Aceptar|Confirmar/OK|Conferma/OK|Bevestigen/OK
close|button|close/dismiss/close-modal|Close/Dismiss|Schließen|Fermer|Cerrar|Fechar|Chiudi|Sluiten
keep_editing|button|keep-editing/continue-editing|Keep editing/Continue editing|Weiter bearbeiten|Continuer à modifier|Seguir editando|Continuar editando|Continua a modificare|Verder bewerken
save_draft|button|save-draft/draft|Save draft/Save as draft|Entwurf speichern|Enregistrer le brouillon|Guardar borrador|Salvar rascunho|Salva bozza|Concept opslaan
delete_draft|button|delete-draft/discard-draft|Delete draft/Discard draft|Entwurf löschen|Supprimer le brouillon|Eliminar borrador|Excluir rascunho|Elimina bozza|Concept verwijderen
publish|button|publish/publish-now/go-live|Publish/Publish now|Veröffentlichen|Publier|Publicar|Publicar|Pubblica|Publiceren
delete_project|button|delete-project/remove-project|Delete project/Delete this project|Projekt löschen|Supprimer le projet|Eliminar proyecto|Excluir projeto|Elimina progetto|Project verwijderen
archive_project|button|archive-project|Archive project/Archive this project|Projekt archivieren|Archiver le projet|Archivar proyecto|Arquivar projeto|Archivia progetto|Project archiveren
rename|button|rename/rename-project|Rename/Rename project|Umbenennen|Renommer|Cambiar nombre|Renomear|Rinomina|Naam wijzigen
confirm_name|input:text|confirm-name/confirm-input/type-to-confirm|Type the project name to confirm/Project name|Projektnamen zur Bestätigung eingeben|Saisissez le nom du projet pour confirmer|Escribe el nombre del proyecto|Digite o nome do projeto|Digita il nome del progetto|Typ de projectnaam ter bevestiging
accept_all|button|accept-all/accept/allow-all/agree|Accept all/Accept all cookies/Allow all/I agree|Alle akzeptieren/Alle Cookies akzeptieren/Zustimmen|Tout accepter/Accepter tous les cookies|Aceptar todas/Aceptar cookies|Aceitar todos/Aceitar cookies|Accetta tutti/Accetta|Alles accepteren/Akkoord
reject_all|button|reject-all/decline/reject/necessary-only|Reject all/Decline/Reject non-essential/Only necessary cookies|Alle ablehnen/Ablehnen/Nur notwendige|Tout refuser/Refuser/Continuer sans accepter|Rechazar todas/Rechazar|Rejeitar todos/Recusar|Rifiuta tutti/Rifiuta|Alles weigeren/Weigeren
manage_cookies|button|manage-cookies/preferences/customize/cookie-settings|Manage preferences/Cookie settings/Customize/More options|Einstellungen verwalten/Anpassen|Personnaliser/Paramétrer les cookies|Configurar/Gestionar preferencias|Gerenciar preferências/Personalizar|Gestisci preferenze/Personalizza|Voorkeuren beheren/Instellingen
save_preferences|button|save-preferences/confirm-choices/save-consent|Save preferences/Confirm my choices/Save settings|Auswahl speichern|Enregistrer mes choix|Guardar preferencias|Salvar preferências|Salva preferenze|Voorkeuren opslaan
analytics_cookies|checkbox|analytics/performance-cookies/statistics|Analytics cookies/Performance cookies|Statistik-Cookies|Cookies analytiques|Cookies analíticas|Cookies de análise|Cookie analitici|Analytische cookies
marketing_cookies|checkbox|marketing/advertising/ads-cookies|Marketing cookies/Advertising cookies|Marketing-Cookies|Cookies marketing|Cookies de marketing|Cookies de marketing|Cookie di marketing|Marketingcookies
subscribe|button|subscribe/newsletter-submit/sign-me-up/join|Subscribe/Sign me up/Join the list|Abonnieren/Anmelden|S'abonner/Je m'inscris|Suscribirse/Suscribirme|Inscrever-se/Assinar|Iscriviti|Abonneren/Inschrijven
unsubscribe|button|unsubscribe/opt-out|Unsubscribe/Opt out|Abbestellen|Se désabonner|Darse de baja|Cancelar inscrição|Annulla iscrizione|Uitschrijven
no_thanks|button|no-thanks/maybe-later/not-now-popup|No thanks/Maybe later/Not now|Nein danke/Später|Non merci/Plus tard|No, gracias/Quizás más tarde|Não, obrigado/Talvez mais tarde|No grazie/Più tardi|Nee, bedankt/Misschien later
home|link|home/logo/home-link|Home|Startseite|Accueil|Inicio|Início|Home|Home
products|link|products/shop/catalog|Products/Shop|Produkte|Produits|Productos|Produtos|Prodotti|Producten
pricing|link|pricing/plans/price|Pricing/Plans/Plans and pricing|Preise|Tarifs|Precios|Preços|Prezzi|Prijzen
features|link|features/product-features|Features|Funktionen|Fonctionnalités|Funciones|Recursos|Funzionalità|Functies
about|link|about/about-us/company|About/About us/Company|Über uns|À propos|Sobre nosotros/Quiénes somos|Sobre nós|Chi siamo|Over ons
blog|link|blog/journal/news|Blog/Journal|Blog/Magazin|Blog|Blog|Blog|Blog|Blog
careers|link|careers/jobs/hiring|Careers/Jobs/We're hiring|Karriere/Jobs|Carrières/Recrutement|Empleo/Trabaja con nosotros|Carreiras/Vagas|Lavora con noi/Carriere|Vacatures/Werken bij
contact|link|contact/contact-us/get-in-touch|Contact/Contact us/Get in touch|Kontakt|Contact/Nous contacter|Contacto/Contáctanos|Contato/Fale conosco|Contatti|Contact
docs|link|docs/documentation/developers|Docs/Documentation/Developers|Dokumentation|Documentation|Documentación|Documentação|Documentazione|Documentatie
help|link|help/support/help-center|Help/Help center/Support|Hilfe/Hilfecenter|Aide/Centre d'aide|Ayuda/Centro de ayuda|Ajuda/Central de ajuda|Aiuto/Centro assistenza|Help/Klantenservice
privacy|link|privacy/privacy-policy|Privacy/Privacy policy|Datenschutz/Datenschutzerklärung|Confidentialité/Politique de confidentialité|Privacidad/Política de privacidad|Privacidade/Política de privacidade|Privacy/Informativa sulla privacy|Privacy/Privacybeleid
terms|link|terms/tos/terms-of-service|Terms/Terms of service/Terms and conditions|AGB/Nutzungsbedingungen|Conditions d'utilisation/CGU|Términos/Condiciones de uso|Termos de uso|Termini di servizio|Voorwaarden/Algemene voorwaarden
cookie_policy|link|cookies/cookie-policy|Cookie policy/Cookies|Cookie-Richtlinie|Politique de cookies|Política de cookies|Política de cookies|Cookie policy|Cookiebeleid
status|link|status/system-status|Status/System status|Status/Systemstatus|État du service|Estado del sistema|Status do sistema|Stato del servizio|Status
changelog|link|changelog/whats-new/releases|Changelog/What's new/Release notes|Neuigkeiten/Versionshinweise|Nouveautés|Novedades|Novidades|Novità|Wat is er nieuw
security|link|security/trust|Security/Trust center|Sicherheit|Sécurité|Seguridad|Segurança|Sicurezza|Beveiliging
press|link|press/newsroom/media|Press/Newsroom|Presse|Presse|Prensa|Imprensa|Stampa|Pers
faq|link|faq/questions|FAQ/Frequently asked questions|FAQ/Häufige Fragen|FAQ/Questions fréquentes|Preguntas frecuentes|Perguntas frequentes|Domande frequenti|Veelgestelde vragen
shipping_info|link|shipping/delivery/shipping-info|Shipping/Delivery information|Versand/Lieferung|Livraison|Envíos|Entrega/Envio|Spedizioni|Verzending
returns|link|returns/refunds/returns-policy|Returns/Returns and refunds|Rücksendungen/Retouren|Retours|Devoluciones|Devoluções/Trocas e devoluções|Resi|Retourneren
integrations|link|integrations/apps/marketplace|Integrations/Apps|Integrationen|Intégrations|Integraciones|Integrações|Integrazioni|Integraties
customers|link|customers/customer-stories/case-studies|Customers/Customer stories|Kunden|Clients|Clientes|Clientes|Clienti|Klanten
account_link|link|account/my-account/profile-link|Account/My account|Mein Konto|Mon compte|Mi cuenta|Minha conta|Il mio account|Mijn account
orders|link|orders/my-orders/order-history|Orders/My orders/Order history|Bestellungen/Meine Bestellungen|Commandes/Mes commandes|Pedidos/Mis pedidos|Pedidos/Meus pedidos|Ordini/I miei ordini|Bestellingen
track_order|link|track-order/tracking/order-status|Track order/Track your order|Sendung verfolgen|Suivre ma commande|Seguir pedido|Rastrear pedido|Traccia ordine|Bestelling volgen
learn_more|link|learn-more/read-more/more-info|Learn more/Read more/See how it works|Mehr erfahren|En savoir plus|Más información|Saiba mais|Scopri di più|Meer informatie
get_started|button|get-started/start-trial/try-free|Get started/Start free trial/Try it free|Jetzt starten/Kostenlos testen|Commencer/Essai gratuit|Empezar/Prueba gratis|Começar/Teste grátis|Inizia/Prova gratis|Aan de slag/Gratis proberen
book_demo|button|book-demo/request-demo/talk-to-sales|Book a demo/Request a demo/Talk to sales|Demo buchen|Réserver une démo|Solicitar una demo|Agendar uma demonstração|Prenota una demo|Demo aanvragen
copy_link|button|copy-link/share-link/copy-url|Copy link/Copy URL|Link kopieren|Copier le lien|Copiar enlace|Copiar link|Copia link|Link kopiëren
share_linkedin|button|share-linkedin/linkedin|Share on LinkedIn/LinkedIn|Auf LinkedIn teilen|Partager sur LinkedIn|Compartir en LinkedIn|Compartilhar no LinkedIn|Condividi su LinkedIn|Delen op LinkedIn
share_x|button|share-x/share-twitter/tweet|Share on X/Tweet|Auf X teilen|Partager sur X|Compartir en X|Compartilhar no X|Condividi su X|Delen op X
comment|textarea|comment/comment-body/reply-text|Comment/Your comment/Write a comment|Kommentar|Commentaire|Comentario|Comentário|Commento|Reactie
post_comment|button|post-comment/submit-comment/reply|Post comment/Submit comment/Reply|Kommentar abschicken|Publier le commentaire|Publicar comentario|Publicar comentário|Pubblica commento|Reactie plaatsen
like|button|like/clap/upvote|Like/Clap|Gefällt mir|J'aime|Me gusta|Curtir|Mi piace|Vind ik leuk
follow|button|follow/follow-author|Follow/Follow author|Folgen|Suivre|Seguir|Seguir|Segui|Volgen
next_article|link|next-article/next-post|Next article/Next post|Nächster Artikel|Article suivant|Siguiente artículo|Próximo artigo|Articolo successivo|Volgend artikel
prev_article|link|prev-article/previous-post|Previous article/Previous post|Vorheriger Artikel|Article précédent|Artículo anterior|Artigo anterior|Articolo precedente|Vorig artikel
save_result|button|save-result/bookmark/save-search-result|Save/Bookmark|Merken|Enregistrer|Guardar|Salvar|Salva|Bewaren
sign_in_again|button|sign-in-again/relogin/reauth|Sign in again/Log in again|Erneut anmelden|Se reconnecter|Volver a iniciar sesión|Entrar novamente|Accedi di nuovo|Opnieuw inloggen
resend_email|button|resend-email/resend-verification|Resend email/Send it again|E-Mail erneut senden|Renvoyer l'e-mail|Reenviar correo|Reenviar e-mail|Invia di nuovo l'email|E-mail opnieuw versturen
change_email|link|change-email/use-another-email|Change email/Use a different email|E-Mail-Adresse ändern|Changer d'adresse e-mail|Cambiar correo|Alterar e-mail|Cambia email|E-mailadres wijzigen
request_access|button|request-access/ask-access|Request access/Ask for access|Zugriff anfordern|Demander l'accès|Solicitar acceso|Solicitar acesso|Richiedi accesso|Toegang aanvragen
switch_account|link|switch-account/other-account|Switch account/Use another account|Konto wechseln|Changer de compte|Cambiar de cuenta|Trocar de conta|Cambia account|Ander account gebruiken
age_yes|button|age-yes/over-18/enter-site|Yes, I am over 18/Enter site|Ja, ich bin über 18|Oui, j'ai plus de 18 ans|Sí, soy mayor de 18|Sim, tenho mais de 18|Sì, ho più di 18 anni|Ja, ik ben ouder dan 18
age_no|button|age-no/under-18/leave-site|No, take me back/Leave|Nein|Non|No|Não|No|Nee
stay_signed_in|button|stay-signed-in/keep-session/extend-session|Stay signed in/Keep me signed in|Angemeldet bleiben|Rester connecté|Seguir conectado|Continuar conectado|Resta connesso|Ingelogd blijven
"""

# Controls a decoy should never be confused with: the concept that does harm when picked.
DESTRUCTIVE = frozenset({
    'delete_account', 'delete', 'remove_item', 'empty_cart', 'discard', 'delete_draft',
    'delete_project', 'bulk_delete', 'remove_avatar', 'sign_out', 'unsubscribe', 'reject_all',
    'archive_project', 'remove_coupon',
})

# Concepts that look alike but mean different things, as decoys for one another.
NEIGHBOURS = {
    'save_changes': ('discard', 'delete_account', 'cancel', 'save_address', 'save_payee', 'save_draft'),
    'save_address': ('save_changes', 'save_payee', 'cancel', 'delete'),
    'save_payee': ('save_changes', 'save_address', 'delete', 'cancel'),
    'save_draft': ('delete_draft', 'publish', 'discard', 'save_changes'),
    'publish': ('save_draft', 'delete_draft', 'discard'),
    'sign_in': ('sign_up', 'google_sign_in', 'apple_sign_in', 'forgot_password', 'show_password'),
    'sign_up': ('sign_in', 'google_sign_in', 'apple_sign_in'),
    'add_to_cart': ('add_to_wishlist', 'buy_now', 'remove_item'),
    'add_to_wishlist': ('add_to_cart', 'buy_now'),
    'buy_now': ('add_to_cart', 'add_to_wishlist'),
    'checkout': ('continue_shopping', 'update_cart', 'empty_cart'),
    'place_order': ('back', 'cancel', 'apply_coupon'),
    'apply_coupon': ('remove_coupon', 'place_order'),
    'continue': ('back', 'cancel', 'skip'),
    'back': ('continue', 'cancel'),
    'confirm': ('cancel', 'close', 'delete_project'),
    'archive_project': ('delete_project', 'rename', 'cancel'),
    'rename': ('delete_project', 'archive_project'),
    'edit': ('delete', 'view', 'duplicate', 'archive'),
    'view': ('edit', 'delete', 'download'),
    'duplicate': ('delete', 'edit'),
    'accept_all': ('reject_all', 'manage_cookies', 'save_preferences'),
    'reject_all': ('accept_all', 'manage_cookies'),
    'manage_cookies': ('accept_all', 'reject_all'),
    'save_preferences': ('accept_all', 'reject_all'),
    'subscribe': ('unsubscribe', 'no_thanks', 'close'),
    'no_thanks': ('subscribe',),
    'invite': ('bulk_delete', 'export_csv'),
    'export_csv': ('bulk_delete', 'invite'),
    'export_data': ('delete_account',),
    'upload_avatar': ('remove_avatar',),
    'new_project': ('delete_project', 'archive_project'),
    'post_comment': ('cancel',),
    'apply_filters': ('clear_filters',),
    'clear_filters': ('apply_filters',),
    'verify': ('resend_code',),
    'change_password': ('delete_account', 'sign_out'),
    'remember_me': ('newsletter_optin', 'accept_terms'),
    'accept_terms': ('newsletter_optin', 'remember_me'),
    'newsletter_optin': ('accept_terms',),
    'email_notifications': ('push_notifications', 'weekly_digest'),
    'push_notifications': ('email_notifications', 'weekly_digest'),
    'weekly_digest': ('email_notifications', 'push_notifications'),
    'ship_standard': ('ship_express',),
    'ship_express': ('ship_standard',),
    'email': ('confirm_password', 'full_name', 'phone', 'username'),
    'password': ('confirm_password', 'email'),
    'confirm_password': ('password',),
    'first_name': ('last_name', 'full_name'),
    'last_name': ('first_name', 'full_name'),
    'city': ('postcode', 'address'),
    'postcode': ('city', 'address'),
    'next_page': ('prev_page',),
    'prev_page': ('next_page',),
    'learn_more': ('get_started', 'book_demo'),
    'get_started': ('book_demo', 'learn_more', 'sign_in_link'),
    'privacy': ('terms', 'cookie_policy'),
    'terms': ('privacy', 'cookie_policy'),
}


class Concept:
    __slots__ = ('name', 'kind', 'slugs', 'words', 'destructive')

    def __init__(self, name, kind, slugs, words, destructive):
        self.name = name
        self.kind = kind
        self.slugs = slugs
        self.words = words
        self.destructive = destructive

    def variants(self, lang):
        return self.words.get(lang) or self.words['en']

    def __repr__(self):
        return '<Concept ' + self.name + '>'


def _load():
    out = {}
    for line in _RAW.strip().splitlines():
        parts = line.split('|')
        if len(parts) != 3 + len(LANGS):
            raise ValueError('bad lexicon line: ' + line)
        name, kind, slugs = parts[:3]
        words = {}
        for lang, text in zip(LANGS, parts[3:], strict=True):
            if text.strip():
                words[lang] = [w.strip() for w in text.split('/') if w.strip()]
        out[name] = Concept(name, kind, slugs.split('/'), words, name in DESTRUCTIVE)
    return out


CONCEPTS = _load()

# Headings and short texts, by key, per language.
_PHRASES = """
h_login|Sign in to {brand}/Welcome back/Log in to your account|Bei {brand} anmelden/Willkommen zurück|Connexion à {brand}/Bon retour|Inicia sesión en {brand}/Bienvenido de nuevo|Entre no {brand}/Bem-vindo de volta|Accedi a {brand}/Bentornato|Inloggen bij {brand}/Welkom terug
h_signup|Create your account/Get started with {brand}/Sign up for {brand}|Konto erstellen/Registrieren Sie sich bei {brand}|Créez votre compte/Inscrivez-vous sur {brand}|Crea tu cuenta/Regístrate en {brand}|Crie sua conta/Cadastre-se no {brand}|Crea il tuo account/Registrati su {brand}|Maak je account aan/Registreer bij {brand}
h_cart|Shopping cart/Your cart/Your bag|Warenkorb/Ihr Warenkorb|Panier/Votre panier|Carrito/Tu carrito|Carrinho/Seu carrinho|Carrello/Il tuo carrello|Winkelwagen/Je winkelwagen
h_checkout|Checkout/Secure checkout|Kasse/Sichere Kasse|Paiement/Commande sécurisée|Pago/Finalizar compra|Finalizar compra/Pagamento|Cassa/Pagamento sicuro|Afrekenen/Veilig afrekenen
h_contact_info|Contact information/Contact|Kontaktdaten|Coordonnées|Información de contacto|Informações de contato|Informazioni di contatto|Contactgegevens
h_shipping|Shipping address/Delivery address|Lieferadresse/Versandadresse|Adresse de livraison|Dirección de envío|Endereço de entrega|Indirizzo di spedizione|Verzendadres/Afleveradres
h_shipping_method|Shipping method/Delivery options|Versandart|Mode de livraison|Método de envío|Método de entrega|Metodo di spedizione|Verzendmethode
h_payment|Payment/Payment method|Zahlung/Zahlungsart|Paiement/Moyen de paiement|Pago/Método de pago|Pagamento/Forma de pagamento|Pagamento/Metodo di pagamento|Betaling/Betaalmethode
h_summary|Order summary/Summary|Bestellübersicht/Zusammenfassung|Récapitulatif de la commande|Resumen del pedido|Resumo do pedido|Riepilogo ordine|Besteloverzicht
h_products|All products/Shop all/New arrivals|Alle Produkte/Neuheiten|Tous les produits/Nouveautés|Todos los productos/Novedades|Todos os produtos/Novidades|Tutti i prodotti/Nuovi arrivi|Alle producten/Nieuw binnen
h_filters|Filters/Refine results|Filter|Filtres|Filtros|Filtros|Filtri|Filters
h_brand|Brand|Marke|Marque|Marca|Marca|Marca|Merk
h_settings|Settings/Account settings|Einstellungen/Kontoeinstellungen|Paramètres/Paramètres du compte|Configuración/Ajustes de la cuenta|Configurações/Configurações da conta|Impostazioni/Impostazioni account|Instellingen/Accountinstellingen
h_profile|Profile/Public profile|Profil|Profil|Perfil|Perfil|Profilo|Profiel
h_notifications|Notifications/Email preferences|Benachrichtigungen|Notifications|Notificaciones|Notificações|Notifiche|Meldingen
h_security|Security/Password and security|Sicherheit|Sécurité|Seguridad|Segurança|Sicurezza|Beveiliging
h_danger|Danger zone/Delete account|Gefahrenbereich|Zone de danger|Zona de peligro|Zona de perigo|Zona pericolosa|Gevarenzone
h_members|Members/Team members/People|Mitglieder|Membres|Miembros|Membros|Membri|Leden
h_dashboard|Dashboard/Overview|Dashboard/Übersicht|Tableau de bord/Vue d'ensemble|Panel/Resumen|Painel/Visão geral|Dashboard/Panoramica|Dashboard/Overzicht
h_activity|Recent activity/Activity|Letzte Aktivitäten|Activité récente|Actividad reciente|Atividade recente|Attività recenti|Recente activiteit
h_projects|Projects/Your projects|Projekte|Projets|Proyectos|Projetos|Progetti|Projecten
h_search|Search results/Results for "{query}"|Suchergebnisse|Résultats de recherche|Resultados de búsqueda|Resultados da pesquisa|Risultati della ricerca|Zoekresultaten
h_wizard_account|Create your account/Your details|Ihr Konto/Ihre Daten|Votre compte/Vos informations|Tu cuenta/Tus datos|Sua conta/Seus dados|Il tuo account/I tuoi dati|Je account/Je gegevens
h_wizard_team|Tell us about your team/About your team|Erzählen Sie uns von Ihrem Team|Parlez-nous de votre équipe|Cuéntanos sobre tu equipo|Fale sobre sua equipe|Parlaci del tuo team|Vertel ons over je team
h_wizard_invite|Invite your teammates/Bring your team|Laden Sie Ihr Team ein|Invitez votre équipe|Invita a tu equipo|Convide sua equipe|Invita il tuo team|Nodig je team uit
h_wizard_plan|Choose a plan/Pick your plan|Tarif wählen|Choisissez une offre|Elige un plan|Escolha um plano|Scegli un piano|Kies een abonnement
h_cookie|We value your privacy/This site uses cookies/Cookie preferences|Wir respektieren Ihre Privatsphäre/Diese Website verwendet Cookies|Nous respectons votre vie privée/Ce site utilise des cookies|Valoramos tu privacidad/Este sitio usa cookies|Valorizamos sua privacidade/Este site usa cookies|Rispettiamo la tua privacy/Questo sito usa i cookie|Wij respecteren je privacy/Deze site gebruikt cookies
p_cookie|We use cookies to improve your experience and for analytics./We and our partners use cookies to personalise content.|Wir verwenden Cookies, um Ihr Erlebnis zu verbessern.|Nous utilisons des cookies pour améliorer votre expérience.|Usamos cookies para mejorar tu experiencia.|Usamos cookies para melhorar sua experiência.|Usiamo i cookie per migliorare la tua esperienza.|We gebruiken cookies om je ervaring te verbeteren.
h_newsletter|Get 10% off your first order/Join our newsletter/Stay in the loop|10 % Rabatt auf Ihre erste Bestellung/Newsletter abonnieren|-10 % sur votre première commande/Inscrivez-vous à la newsletter|10 % de descuento en tu primer pedido/Únete a nuestro boletín|10% de desconto no primeiro pedido/Assine nossa newsletter|10% di sconto sul primo ordine/Iscriviti alla newsletter|10% korting op je eerste bestelling/Schrijf je in voor de nieuwsbrief
h_delete_dialog|Delete project?/Are you sure?|Projekt löschen?/Sind Sie sicher?|Supprimer le projet ?/Êtes-vous sûr ?|¿Eliminar proyecto?/¿Estás seguro?|Excluir projeto?/Tem certeza?|Eliminare il progetto?/Sei sicuro?|Project verwijderen?/Weet je het zeker?
h_unsaved|Unsaved changes/Leave without saving?|Ungespeicherte Änderungen|Modifications non enregistrées|Cambios sin guardar|Alterações não salvas|Modifiche non salvate|Niet-opgeslagen wijzigingen
h_session|Your session has expired/You have been signed out|Ihre Sitzung ist abgelaufen|Votre session a expiré|Tu sesión ha caducado|Sua sessão expirou|La tua sessione è scaduta|Je sessie is verlopen
h_verify|Check your inbox/Verify your email|Prüfen Sie Ihr Postfach|Vérifiez votre boîte de réception|Revisa tu bandeja de entrada|Verifique sua caixa de entrada|Controlla la tua casella|Controleer je inbox
h_denied|You don't have access to this page/Access denied|Kein Zugriff auf diese Seite|Vous n'avez pas accès à cette page|No tienes acceso a esta página|Você não tem acesso a esta página|Non hai accesso a questa pagina|Je hebt geen toegang tot deze pagina
h_age|Are you over 18?/Age verification|Sind Sie über 18?|Avez-vous plus de 18 ans ?|¿Eres mayor de 18 años?|Você tem mais de 18 anos?|Hai più di 18 anni?|Ben je ouder dan 18?
h_idle|Are you still there?/Still there?|Sind Sie noch da?|Êtes-vous toujours là ?|¿Sigues ahí?|Você ainda está aí?|Sei ancora lì?|Ben je er nog?
h_features|Everything you need/Why teams choose {brand}|Alles, was Sie brauchen|Tout ce dont vous avez besoin|Todo lo que necesitas|Tudo o que você precisa|Tutto ciò che ti serve|Alles wat je nodig hebt
h_hero|Ship faster with {brand}/The simplest way to run your team|Schneller liefern mit {brand}|Livrez plus vite avec {brand}|Entrega más rápido con {brand}|Entregue mais rápido com {brand}|Consegna più veloce con {brand}|Sneller leveren met {brand}
h_related|Related articles/Keep reading|Ähnliche Artikel|Articles similaires|Artículos relacionados|Artigos relacionados|Articoli correlati|Gerelateerde artikelen
h_comments|Comments/Join the discussion|Kommentare|Commentaires|Comentarios|Comentários|Commenti|Reacties
h_reset|Reset your password/Forgot your password?|Passwort zurücksetzen|Réinitialiser votre mot de passe|Restablecer tu contraseña|Redefinir sua senha|Reimposta la password|Wachtwoord opnieuw instellen
h_twofactor|Two-step verification/Enter your code|Bestätigung in zwei Schritten|Vérification en deux étapes|Verificación en dos pasos|Verificação em duas etapas|Verifica in due passaggi|Verificatie in twee stappen
h_orders|Your orders/Order history|Ihre Bestellungen|Vos commandes|Tus pedidos|Seus pedidos|I tuoi ordini|Je bestellingen
f_company|Company|Unternehmen|Entreprise|Empresa|Empresa|Azienda|Bedrijf
f_resources|Resources|Ressourcen|Ressources|Recursos|Recursos|Risorse|Bronnen
f_legal|Legal|Rechtliches|Mentions légales|Legal|Jurídico|Note legali|Juridisch
f_support|Support/Help|Hilfe|Assistance|Soporte|Suporte|Assistenza|Klantenservice
"""


def _load_phrases():
    out = {}
    for line in _PHRASES.strip().splitlines():
        parts = line.split('|')
        if len(parts) != 1 + len(LANGS):
            raise ValueError('bad phrase line: ' + line)
        words = {}
        for lang, text in zip(LANGS, parts[1:], strict=True):
            if text.strip():
                words[lang] = [w.strip() for w in text.split('/') if w.strip()]
        out[parts[0]] = words
    return out


PHRASES = _load_phrases()

# Entities. The train and test splits draw from different halves, so the test set has
# names the model never saw.
PRODUCTS = {
    'train': ['Blue Oxford Shirt', 'Canvas Tote Bag', 'Merino Crew Sweater', 'Trail Runner 2',
              'Ceramic Pour-Over Set', 'Linen Throw Pillow', 'Walnut Desk Organizer',
              'Wireless Earbuds Pro', 'Stainless Water Bottle', 'Leather Card Holder',
              'Wool Beanie', 'Bamboo Cutting Board', 'Cotton Hoodie', 'Rain Shell Jacket',
              'Desk Lamp Mini', 'Yoga Mat Plus', 'Cast Iron Skillet', 'Graphic Tee',
              'Denim Jacket', 'Running Socks 3-Pack', 'Travel Backpack 28L', 'Scented Candle',
              'French Press', 'Phone Case Clear', 'Sunglasses Classic', 'Linen Shirt',
              'Chino Shorts', 'Silk Scarf', 'Hiking Boots', 'Wall Clock', 'Throw Blanket',
              'Espresso Cups Set', 'Bike Helmet', 'Notebook A5', 'Fountain Pen'],
    'test': ['Alpine Fleece Vest', 'Copper Kettle', 'Oak Bookshelf', 'Velvet Armchair',
             'Smart Thermostat', 'Gardening Gloves', 'Marble Coaster Set', 'Chef Knife 8in',
             'Cordless Drill', 'Picnic Blanket', 'Glass Teapot', 'Rope Basket',
             'Ankle Boots', 'Puffer Coat', 'Mechanical Keyboard', 'Monitor Arm',
             'Tennis Racket', 'Swim Goggles', 'Camping Lantern', 'Herb Planter'],
}
FIRST = {
    'train': ['Maya', 'Leo', 'Ana', 'Sam', 'Priya', 'Tom', 'Lena', 'Omar', 'Chloe', 'Ivan', 'Nora',
              'Jonas', 'Aisha', 'Lucas', 'Mia', 'Ravi', 'Emma', 'Diego', 'Hana', 'Felix'],
    'test': ['Yusuf', 'Ingrid', 'Kofi', 'Beatriz', 'Tariq', 'Sofia', 'Mateo', 'Freya', 'Kenji',
             'Amara', 'Lars', 'Zara'],
}
LAST = {
    'train': ['Okafor', 'Martins', 'Lima', 'Chen', 'Patel', 'Becker', 'Rossi', 'Novak', 'Haddad',
              'Kim', 'Silva', 'Jensen', 'Moreau', 'Garcia', 'Nakamura', 'Fischer', 'Brown', 'Costa'],
    'test': ['Adeyemi', 'Lindqvist', 'Kowalski', 'Fernandes', 'Takahashi', 'Mensah', 'Dubois',
             'Horvat', 'Iqbal', 'Svensson'],
}
PROJECTS = {
    'train': ['Website redesign', 'Q3 roadmap', 'Mobile app', 'Billing migration', 'Onboarding flow',
              'Data warehouse', 'Brand refresh', 'Support portal', 'Holiday campaign', 'API v2',
              'Design system', 'Customer survey', 'Partner program', 'Infra upgrade'],
    'test': ['Pricing experiment', 'Warehouse automation', 'Loyalty program', 'Analytics revamp',
             'Store opening', 'Security audit', 'Hiring plan'],
}
SYLLABLES = {
    'train': ['ta', 'lo', 'mi', 'ra', 'ven', 'kor', 'lu', 'by', 'za', 'nex', 'po', 'fi', 'dra',
              'qui', 'mo', 'sa', 'tel', 'ri', 'van', 'cle'],
    'test': ['gro', 'pex', 'ul', 'wy', 'har', 'bo', 'zen', 'tri', 'ok', 'sil', 'mu', 'dex'],
}
BRAND_SUFFIX = ['', '', 'ly', 'io', 'hub', 'base', 'board', 'works', 'lab', 'go', 'ify']
CATEGORIES = ['Shirts', 'Outerwear', 'Shoes', 'Accessories', 'Home', 'Kitchen', 'Bags', 'Sale',
              'Electronics', 'Outdoor', 'Beauty', 'Kids']
BRAND_NAMES = ['Northwind', 'Acme', 'Fabrikam', 'Contoso', 'Tailspin', 'Wingtip', 'Litware',
               'Adatum', 'Proseware', 'Lucerne', 'Coho', 'Margie']
ARTICLE_TITLES = {
    'train': ['How we cut build times by 40%', 'A practical guide to feature flags',
              'Designing for accessibility', 'Our 2026 product roadmap', 'Scaling Postgres at work',
              'Lessons from our first outage', 'Why we moved to a monorepo',
              'Writing better commit messages', 'The case for boring technology'],
    'test': ['Measuring developer productivity', 'Migrating a legacy checkout',
             'Notes on incident reviews', 'Running a remote offsite', 'Caching without tears'],
}
ROLES_TEXT = ['Admin', 'Member', 'Guest', 'Owner', 'Viewer', 'Billing']
STATUS_TEXT = ['Active', 'Invited', 'Suspended', 'Pending']
