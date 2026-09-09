# cskel27.npz

ARDY's 27-joint character skeleton (`cskel27`): joint names, hierarchy, bind
matrices, and a bind-pose body mesh with linear-blend-skinning weights.

Taken verbatim from `ardy/assets/skeletons/cskel27/skin_standard.npz` in
https://github.com/nv-tlabs/ardy (NVIDIA, Apache-2.0). Only the filename
changed.

It is the rig the `kimodo` job type animates: bone lengths match a generated
result to 1.6e-07, so it is the skeleton itself rather than an approximation of
it. Shipping it is what lets the add-on turn a bare `.npz` of joint transforms
into an armature and a body without asking the backend for anything else.

The ARDY *model weights* are covered by a separate NVIDIA Open Model License.
No weights are bundled here — this is an asset file from the Apache-2.0 repo.
