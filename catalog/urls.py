from django.urls import path

from catalog import admin_views, views

urlpatterns = [
    path("", views.home, name="home"),
    path("gallery/", views.gallery, name="gallery"),
    path("photos/next/", views.next_uncataloged, name="next_uncataloged"),
    path("photos/download/", views.download_photos, name="download_photos"),
    path("photos/shared/", views.log_share, name="log_share"),
    path("photos/<int:pk>/", views.photo_detail, name="photo_detail"),
    path("photos/<int:pk>/image/<str:size>/", views.photo_image, name="photo_image"),
    path("photos/<int:pk>/rotate/", views.rotate_photo, name="rotate_photo"),
    path("photos/<int:pk>/crop/", views.crop_photo, name="crop_photo"),
    path("photos/<int:pk>/crop/detect/", views.crop_detect, name="crop_detect"),
    path("scans/<int:pk>/preview/", views.scan_preview, name="scan_preview"),
    path("photos/<int:pk>/propose/event/", views.propose_event, name="propose_event"),
    path("photos/<int:pk>/propose/date/", views.propose_date, name="propose_date"),
    path("photos/<int:pk>/propose/face/", views.propose_add_face, name="propose_add_face"),
    path("faces/<int:face_pk>/propose/identity/", views.propose_identity, name="propose_identity"),
    path(
        "faces/<int:face_pk>/propose/remove/", views.propose_remove_face, name="propose_remove_face"
    ),
    path("faces/<int:pk>/image/", views.face_image, name="face_image"),
    path("proposals/<int:pk>/vote/", views.vote, name="vote"),
    path("proposals/<int:pk>/decide/", views.decide, name="decide"),
    path("persons/", views.persons, name="persons"),
    path("persons/<int:pk>/", views.person_detail, name="person_detail"),
    path("persons/<int:pk>/avatar/", views.person_avatar, name="person_avatar"),
    path("persons/<int:pk>/rename/", views.person_rename, name="person_rename"),
    path("persons/<int:pk>/claim/", views.person_claim, name="person_claim"),
    path("persons/<int:pk>/unclaim/", views.person_unclaim, name="person_unclaim"),
    path("activity/", views.my_activity, name="my_activity"),
    # Administration
    path("admin/", admin_views.admin_home, name="admin_home"),
    path("admin/event-types/", admin_views.event_types, name="admin_event_types"),
    path("admin/event-types/<int:pk>/", admin_views.event_type_edit, name="admin_event_type_edit"),
    path("admin/thresholds/", admin_views.thresholds, name="admin_thresholds"),
    path("admin/review/", admin_views.review_queue, name="admin_review_queue"),
    path("admin/review/<int:pk>/ok/", admin_views.review_mark_ok, name="admin_review_ok"),
    path(
        "admin/photos/<int:pk>/discard/",
        admin_views.photo_toggle_discard,
        name="admin_photo_toggle_discard",
    ),
    path(
        "admin/photos/<int:pk>/not-duplicate/",
        admin_views.photo_not_duplicate,
        name="admin_photo_not_duplicate",
    ),
    path("admin/photos/<int:pk>/delete/", admin_views.photo_delete, name="admin_photo_delete"),
    path("admin/scans/<int:pk>/delete/", admin_views.scan_delete, name="admin_scan_delete"),
    path(
        "admin/scans/<int:pk>/reprocess/", admin_views.scan_reprocess, name="admin_scan_reprocess"
    ),
    path("admin/merge/", admin_views.merge, name="admin_merge"),
]
